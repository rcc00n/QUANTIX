"""Self-contained bounded ZIP exports of genuine records, never generated conclusions."""

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select

from arbiq.analytics.contracts import (
    CSV_COLUMNS,
    EXPORT_FILES,
    AnalyticsExportManifest,
    AnalyticsExportMetrics,
)
from arbiq.analytics.service import analytics
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    Asset,
    Exchange,
    FeeVersion,
    FXQuote,
    InventoryTransfer,
    Journal,
    LedgerEntry,
    Market,
    NotificationAttempt,
    Opportunity,
    OrderBookSnapshot,
    PaperFill,
    PaperOrder,
    PaperTrade,
    Signal,
    SimulationEvent,
    StrategyVersion,
    SystemEvent,
    VirtualAccount,
    VirtualDeposit,
)
from arbiq.notifications.models import AlertDecision, NotificationEvent, NotificationOutbox
from arbiq.opportunities.contracts import CALCULATION_VERSION
from arbiq.paper_trading.lifecycle import lifecycle_rank
from arbiq.paper_trading.models import ExecutionObservation
from arbiq.portfolio.contracts import ACCOUNTING_VERSION
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint
from arbiq.settings.models import TerminalSettingsVersion

# Wide immutable evaluation/book JSON can be hundreds of KiB per row. Bound the
# decoded cursor batch independently of the total export record/byte limits.
BATCH_ROWS = 32
MAX_ROWS_PER_FILE = 100_000
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_GENERATION_SECONDS = 90
SECRET_KEYS = re.compile(
    r"(^|_)(password|passwd|secret|cookie|authorization|auth|api_key|apikey|chat_id|chatid|"
    r"access_token|refresh_token|bot_token|session_token|api_token|id_token|"
    r"private_key|credentials?)(_|$)|^token$",
    re.I,
)
SECRET_QUERY = re.compile(r"(?i)(password|api[_-]?key|token|authorization)=([^\s&]+)")
URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", re.I)


def exact(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {str(k): exact(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [exact(v) for v in value]
    return value


@lru_cache(maxsize=2048)
def secret_key(key):
    normalized = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key.replace("-", "_"))
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return bool(SECRET_KEYS.search(normalized))


def sanitize(value):
    """Retain reproducible public evidence, redact credential-bearing arbitrary metadata."""
    # Normalize each node exactly once. Calling recursive exact() here used to
    # recopy every descendant at every nesting level of large retained evidence.
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, UUID)):
        return exact(value)
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if secret_key(str(k)) else sanitize(v) for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [sanitize(x) for x in value]
    if isinstance(value, str):
        value = SECRET_QUERY.sub(r"\1=[REDACTED]", value)
        return URL_USERINFO.sub(r"\1[REDACTED]@", value)
    return value


def record(row):
    # SQLAlchemy attributes are extracted on the event-loop thread. Nested public
    # evidence stays referenced until one bounded worker serializes this row.
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = (
            exact(value) if isinstance(value, (Decimal, datetime, UUID)) else value
        )
    return result


async def sequential_thread(function, *args, **kwargs):
    """Drain one worker before cancellation may close its ZIP member or file.

    No queue/fanout is created: callers await every row before fetching the next.
    Shield alone is insufficient because cancellation would otherwise close the
    member while a still-running thread writes to it.
    """
    job = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            job.result()
        except BaseException:
            pass
        raise


def json_text(value):
    return json.dumps(sanitize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cell(value, *, financial=False):
    if value is None:
        return ""
    if financial and isinstance(value, (float, bool)):
        raise ValueError("Financial export fields require exact decimals, never binary floats")
    if isinstance(value, (dict, list, tuple)):
        return json_text(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if financial and isinstance(value, str):
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise ValueError("Non-finite financial export value")
        return format(parsed, "f")
    if isinstance(value, (datetime, UUID)):
        return exact(value)
    value = str(sanitize(value))
    # Numeric strings retain their real signs; text cannot execute spreadsheet formulas.
    if value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        try:
            parsed = Decimal(value)
            numeric = parsed.is_finite()
        except Exception:
            numeric = False
        if not numeric:
            value = "'" + value
    return value


def column_definition(name):
    meanings = {
        "id": "Stable source-record UUID; join to corresponding *_id fields.",
        "record_json": "Complete sanitized retained source fields; decimals stay exact strings.",
        "quantity": "Exact units of the joined asset, never CAD unless the asset is CAD.",
        "price": "Quote-currency units per base-asset unit; simulated modeled execution price.",
        "status": "Recorded lifecycle state; cutoff-derived/unknown states are explicitly labeled.",
        "reason": "Recorded reason code/text, not an AI conclusion.",
        "account_role": "available/reserved inventory or equity/expense counteraccount.",
        "role": "available/reserved inventory or equity/expense counteraccount.",
        "side": "buy consumes quote/acquires base; sell consumes base/acquires quote.",
        "reaction_delay_ms": "Configured counterfactual human reaction delay, milliseconds.",
        "calculation_version": "Original deterministic calculation version used by this record.",
        "source": "Recorded data/assumption origin; not an eligibility guarantee.",
        "quality": "Retained data-quality classification at observation time.",
        "asset": "Asset symbol or UUID; join assets.csv for canonical identity and units.",
        "exchange": "Exchange slug; public data access does not establish trading eligibility.",
        "fee_asset": "Fee-asset UUID; join assets.csv for exact currency identity.",
        "quote_asset": "Quote-asset symbol; canonical identity comes from linked market metadata.",
        "fee_quantity": "Fee amount in fee_asset units, separately disclosed from CAD value.",
        "bids": "Retained [price,quantity] arrays, descending price; price in quote/base units.",
        "asks": "Retained [price,quantity] arrays, ascending price; price in quote/base units.",
        "canonical_key": "Normalized identity; wrapped/ambiguous assets stay distinct.",
        "native_symbol": "Exchange-native symbol; text never proves asset equivalence.",
    }
    if name in meanings:
        return meanings[name]
    if name.endswith("_json") or name in (
        "details",
        "provenance",
        "evidence",
        "payload",
        "settings",
        "assumptions",
        "metadata_json",
    ):
        return (
            "Sanitized structured source evidence; IDs, exact units and limitations are retained."
        )
    if name.endswith("_id"):
        return "Stable linked record UUID; corresponding raw reconstruction table is included."
    if name.endswith("_at") or name == "as_of":
        return "ISO-8601 UTC timestamp; source/receipt/effective time remain distinct."
    if name.endswith("_cad") or name == "cad_value":
        return "Exact Canadian dollars; empty means unavailable, never zero."
    if name.endswith("_pct"):
        return "Exact percentage points (0.5 means0.5%, not50%)."
    if name.endswith("_rate") or name == "rate":
        return "Exact fractional fee rate or explicitly joined FX quote units per base unit."
    if name.endswith("_quantity"):
        return "Exact units of the referenced asset; inspect asset/market joins."
    return "Retained named source-model field; complete context is preserved in record_json."


class Archive:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
        self.files = []
        self.total_bytes = 0

    async def csv(self, name, columns, rows):
        digest = hashlib.sha256()
        size = count = 0
        line = io.StringIO(newline="")
        writer = csv.writer(line, lineterminator="\r\n")
        with self.zip.open(name, "w") as target:

            def emit(values, *, header=False):
                nonlocal size
                if not header:
                    values = [
                        cell(
                            values.get(column),
                            financial=(
                                column.endswith(("_cad", "_pct", "_quantity", "_rate"))
                                or column in ("quantity", "price", "rate", "quote_notional")
                            ),
                        )
                        for column in columns
                    ]
                line.seek(0)
                line.truncate(0)
                writer.writerow(values)
                data = line.getvalue().encode("utf-8")
                size += len(data)
                self.total_bytes += len(data)
                if self.total_bytes > MAX_UNCOMPRESSED_BYTES:
                    raise HTTPException(
                        413, "Export exceeds256MiB; select a shorter reporting period"
                    )
                digest.update(data)
                target.write(data)

            await sequential_thread(emit, columns, header=True)
            async for row in rows:
                count += 1
                if count > MAX_ROWS_PER_FILE:
                    raise HTTPException(
                        413, "Export exceeds100000rows/file; select a shorter period"
                    )
                await sequential_thread(emit, row)
        self.files.append(
            {
                "name": name,
                "sha256": digest.hexdigest(),
                "bytes": size,
                "records": count,
                "columns": list(columns),
            }
        )

async def records(db, query):
    result = await db.stream_scalars(query.execution_options(yield_per=BATCH_ROWS))
    async for row in result:
        yield record(row)


