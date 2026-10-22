"""Complete retained-data snapshot. Explicit scopes, exact values, bounded cursors/disk.

This is a separate envelope from the stable custom quantix-export-v1 contract.
No financial records or market evidence are modified by an export.
"""

import csv
import hashlib
import io
import itertools
import json
import re
import shutil
import time
import zipfile
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import JSON, func, or_, select, text

from arbiq.analytics.evidence_codec import CODEC, EvidenceStore, compact
from arbiq.analytics.export import cell, exact, sanitize, secret_key, sequential_thread
from arbiq.analytics.export_budget import (
    MAX_TEMP_BYTES,
    MAX_ZIP_BYTES,
    ExportBudgetExceeded,
    ExportCancelled,
)
from arbiq.analytics.export_selection import AnalysisSelection
from arbiq.analytics.export_views import (
    OPPORTUNITY_COLUMNS,
    PORTFOLIO_COLUMNS,
    readable_projection,
    write_coverage,
)
from arbiq.db.base import Base
from arbiq.market_data.timing import TIMING_EXPORT_NOTES

SCHEMA = "quantix-full-v2"
BATCH = 8
MIN_FREE_BYTES = 6 * 1024**3
MAX_OUTPUT_BYTES = 24 * 1024**3

# New tables must receive an explicit policy; schema growth must never silently
# omit data or accidentally export authentication/private communication records.
GROUPS = {
    "portfolios": "paper_sessions paper_controls virtual_accounts virtual_deposits "
    "balance_snapshots portfolio_positions funding_experiments funding_proposals",
    "executions": "paper_trades paper_orders paper_fills simulation_events "
    "execution_observations inventory_transfers paper_rebalance_requests "
    "journals ledger_entries accounting_movements actual_trades",
    "opportunities": "opportunities arbitrage_routes arbitrage_evaluations "
    "arbitrage_lifecycle_events",
    "market_data": "assets market_observations order_book_snapshots fx_quotes "
    "latest_market_books market_data_samples market_quality_events",
    "exchanges": "exchanges markets exchange_config_versions exchange_health fee_versions",
    "signals": "signals notification_attempts alert_decisions alert_lifecycle "
    "notification_outbox notification_events",
    "analytics": "portfolio_valuations portfolio_checkpoints daily_accounting_snapshots",
    "settings": "strategy_versions strategy_heads terminal_settings_versions",
    "system_health": "system_events collector_runs report_exports",
}
TABLE_GROUP = {name: group for group, names in GROUPS.items() for name in names.split()}
EXCLUDED_TABLES = {
    "users": "Authentication and personal identity; never exported.",
    "auth_sessions": "Session cookies/hashes; never exported.",
    "login_attempts": "Login identity/client fingerprints; never exported.",
    "telegram_challenges": "Private pairing challenges and identities; never exported.",
    "telegram_bindings": "Private Telegram destinations and identities; never exported.",
    "notification_worker_state": (
        "Private Telegram update offsets/runtime state; health remains in system events."
    ),
}
PRIVATE_COLUMNS = {
    "user_id",
    "actor_user_id",
    "acknowledged_by",
    "binding_id",
    "message",
    "provider_message_id",
    "user_reference",
    "idempotency_key",
    "deduplication_key",
}
PRIVATE_KEYS = re.compile(
    r"(^|_)(user_?id|username|email|phone|chat_?id|telegram|binding_?id|destination|"
    r"message|text|acknowledged_by|actor_user_id|user_reference|client_hash)(_|$)",
    re.I,
)
BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


@lru_cache(maxsize=2048)
def private_key(key):
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.replace("-", "_"))
    return bool(PRIVATE_KEYS.search(normalized)) or secret_key(key)


def public_value(value):
    """Apply the established secret policy plus private identity/message exclusions."""
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if private_key(str(k)) else public_value(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [public_value(v) for v in value]
    if isinstance(value, (Decimal, datetime, UUID)):
        return exact(value)
    if isinstance(value, str):
        return EMAIL.sub("[REDACTED_EMAIL]", BOT_TOKEN.sub("[REDACTED_TOKEN]", sanitize(value)))
    return value


def public_record(value):
    return public_value(value)


def scope_for(table, owner, seen=frozenset()):
    """Follow financial ownership joins; public scanner/registry evidence is shared."""
    if table.name in seen:
        return None
    if "user_id" in table.c:
        return table.c.user_id == owner
    if table.name == "system_events":
        return or_(table.c.actor_user_id == owner, table.c.actor_user_id.is_(None))
    for column in table.c:
        for fk in column.foreign_keys:
            parent = fk.column.table
            if parent.name in EXCLUDED_TABLES:
                continue
            parent_scope = scope_for(parent, owner, seen | {table.name})
            if parent_scope is not None:
                return column.in_(select(fk.column).where(parent_scope))
    return None


def dataset_query(table, owner):
    columns = [c for c in table.c if c.name not in PRIVATE_COLUMNS]
    query = select(*columns)
    scope = scope_for(table, owner)
    if scope is not None:
        query = query.where(scope)
    # PK ordering uses existing indexes and permits a bounded server-side cursor.
    return columns, query.order_by(*table.primary_key.columns), scope is not None


CODED_TABLES = {
    "arbitrage_evaluations",
    "arbitrage_routes",
    "portfolio_checkpoints",
    "market_quality_events",
    "market_data_samples",
    "market_observations",
    "order_book_snapshots",
    "latest_market_books",
}


class FullArchive:
    def __init__(self, path, progress, *, analysis=False):
        self.path, self.progress = Path(path), progress
        self.zip = zipfile.ZipFile(
            path, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
        )
        self.files = []
        self.bytes = 0
        self.last_check = 0.0
        self.store = EvidenceStore(self.path.with_suffix(".evidence.sqlite"))
        self.selection = AnalysisSelection(self.store) if analysis else None
        self.temp_peak = self.store.path.stat().st_size

    def check(self):
        if self.path.with_suffix(".cancel").exists():
            raise ExportCancelled("Export cancelled by owner")
        if self.bytes > MAX_OUTPUT_BYTES:
            raise ExportBudgetExceeded("OUTPUT_SAFETY_LIMIT: bounded export output exhausted")
        if self.zip.fp.tell() > MAX_ZIP_BYTES - 131072:
            raise ExportBudgetExceeded(
                "ZIP_BUDGET: measured compressed size requires analysis fallback"
            )
        usage = self.store.path.stat().st_size + self.path.stat().st_size
        self.temp_peak = max(self.temp_peak, usage)
        if usage > MAX_TEMP_BYTES:
            raise ExportBudgetExceeded("TEMP_BUDGET: scratch storage requires analysis fallback")
        if time.monotonic() - self.last_check > 2:
            self.last_check = time.monotonic()
            if shutil.disk_usage(self.path.parent).free < MIN_FREE_BYTES:
                raise RuntimeError("DISK_RESERVE: export stopped to preserve6GiB")

    async def csv(self, name, columns, rows, **metadata):
        digest, semantic = hashlib.sha256(), hashlib.sha256()
        size = count = reduced = 0
        temporal = {}
        json_columns = list(metadata.get("json_columns", ()))
        table = metadata.get("source_table")
        with self.zip.open(name, "w", force_zip64=True) as target:

            def emit(batch, header=False):
                nonlocal size, reduced
                stream = io.StringIO(newline="")
                writer = csv.writer(stream, lineterminator="\r\n")
                if header:
                    writer.writerow(columns)
                for original in batch:
                    row = public_record(original)
                    projection = readable_projection(table, row)
                    if projection is not None:
                        self.store.projection(*projection)
                    if self.selection and table:
                        row, changed = self.selection.apply(table, row, json_columns)
                        reduced += int(changed)
                    semantic.update(
                        (compact({k: row.get(k) for k in json_columns}) + "\n").encode()
                    )
                    values = []
                    for column in columns:
                        value = row.get(column)
                        if column in json_columns and value is not None:
                            if table in CODED_TABLES:
                                value = self.store.encode(value)
                            values.append(compact(value))
                        elif isinstance(value, (dict, list)):
                            values.append(compact(value))
                        else:
                            values.append(cell(value))
                    writer.writerow(values)
                    for key, value in original.items():
                        if isinstance(value, datetime):
                            limits = temporal.setdefault(key, [value, value])
                            limits[0], limits[1] = min(limits[0], value), max(limits[1], value)
                data = stream.getvalue().encode("utf-8")
                digest.update(data)
                size += len(data)
                self.bytes += len(data)
                target.write(data)
                self.check()

            await sequential_thread(emit, [], True)
            batch = []
            try:
                async for row in rows:
                    batch.append(row)
                    count += 1
                    if len(batch) == BATCH:
                        await sequential_thread(emit, batch)
                        batch = []
                        await self.progress(name, count, self.bytes)
                if batch:
                    await sequential_thread(emit, batch)
            finally:
                if hasattr(rows, "aclose"):
                    await rows.aclose()
        entry = {
            "name": name,
            "sha256": digest.hexdigest(),
            "bytes": size,
            "rows": count,
            "columns": list(columns),
            "status": "complete",
            "included_rows": count,
            "omitted_rows": 0,
            "complete_raw_rows": count - reduced,
            "raw_payload_reduced_rows": reduced,
            "json_semantic_sha256": semantic.hexdigest(),
            "json_encoding": CODEC if table in CODED_TABLES else "plain JSON",
            "empty_state": "No retained records in this scope" if not count else None,
            "timestamp_ranges": {
                k: {"min": exact(v[0]), "max": exact(v[1])} for k, v in temporal.items()
            },
            **metadata,
        }
        self.files.append(entry)
        await self.progress(name, count, self.bytes, completed=not metadata.get("auxiliary", False))
        return entry

    async def evidence(self):
        digest, size, count = hashlib.sha256(), 0, 0
        with self.zip.open("evidence/objects.jsonl", "w", force_zip64=True) as target:

            def write():
                nonlocal size, count
                for identifier, _node_hash, body in self.store.objects():
                    data = (f"[{identifier}," + body + "]\n").encode("utf-8")
                    target.write(data)
                    digest.update(data)
                    size += len(data)
                    self.bytes += len(data)
                    count += 1
                    if count % 512 == 0:
                        self.check()
                self.check()

            await sequential_thread(write)
        self.files.append(
            {
                "name": "evidence/objects.jsonl",
                "sha256": digest.hexdigest(),
                "bytes": size,
                "rows": count,
                "format": CODEC,
            }
        )

    async def text(self, name, content):
        def write():
            data = content.encode("utf-8")
            self.zip.writestr(name, data)
            self.bytes += len(data)
            self.check()
            self.files.append(
                {
                    "name": name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "rows": None,
                }
            )

        await sequential_thread(write)

    async def json(self, name, value):
        await self.text(name, json.dumps(public_record(value), ensure_ascii=False, indent=2))

    def close(self):
        try:
            self.zip.close()
        finally:
            self.temp_peak = max(
                self.temp_peak, self.store.path.stat().st_size + self.path.stat().st_size
            )
            self.store.close()
            self.store.path.unlink(missing_ok=True)


async def stream_rows(db, query):
    result = await db.stream(query.execution_options(yield_per=BATCH))
    try:
        async for row in result.mappings():
            yield dict(row)
    finally:
        await result.close()


async def portfolio_metrics(db, owner):
    """Exact all-history SQL reconciliation, independent portfolios never combined."""
    from arbiq.db.models import Journal, LedgerEntry, PaperSession, PaperTrade, VirtualAccount
    from arbiq.paper_trading.models import PaperControl
    from arbiq.portfolio.analytics import performance
    from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint

    summaries = []
    query = (
        select(PaperSession, PaperControl)
        .outerjoin(PaperControl)
        .where(PaperSession.user_id == owner)
    )
    result = await db.stream(query.execution_options(yield_per=BATCH))
    async for session, control in result:
        sid = session.id
        balances = (
            select(
                LedgerEntry.journal_id,
                VirtualAccount.asset_id,
                func.sum(LedgerEntry.quantity).label("quantity"),
            )
            .join(VirtualAccount, VirtualAccount.id == LedgerEntry.account_id)
            .join(Journal, Journal.id == LedgerEntry.journal_id)
            .where(Journal.session_id == sid)
            .group_by(LedgerEntry.journal_id, VirtualAccount.asset_id)
            .subquery()
        )
        unbalanced = await db.scalar(
            select(func.count()).select_from(balances).where(balances.c.quantity != 0)
        )
        inventory = (
            select(VirtualAccount.id, func.sum(LedgerEntry.quantity).label("quantity"))
            .join(LedgerEntry, LedgerEntry.account_id == VirtualAccount.id)
            .where(
                VirtualAccount.session_id == sid, VirtualAccount.role.in_(("available", "reserved"))
            )
            .group_by(VirtualAccount.id)
            .subquery()
        )
        negative = await db.scalar(
            select(func.count()).select_from(inventory).where(inventory.c.quantity < 0)
        )
        outcomes = dict(
            (
                await db.execute(
                    select(PaperTrade.status, func.count())
                    .where(PaperTrade.session_id == sid)
                    .group_by(PaperTrade.status)
                )
            ).all()
        )
        latest = await db.scalar(
            select(PortfolioCheckpoint)
            .where(PortfolioCheckpoint.session_id == sid)
            .order_by(PortfolioCheckpoint.as_of.desc(), PortfolioCheckpoint.id.desc())
            .limit(1)
        )
        sums = (
            (
                await db.execute(
                    select(
                        *[
                            func.sum(getattr(AccountingMovement, k)).label(k)
                            for k in (
                                "realized_trading_cad",
                                "realized_fx_cad",
                                "fees_cad",
                                "expenses_cad",
                                "external_flow_cad",
                            )
                        ]
                    ).where(AccountingMovement.session_id == sid)
                )
            )
            .mappings()
            .one()
        )
        # SUM ignores NULL; disclose unknown counts alongside each known subtotal.
        unknowns = (
            (
                await db.execute(
                    select(
                        *[
                            func.count().filter(getattr(AccountingMovement, k).is_(None)).label(k)
                            for k in sums
                        ]
                    ).where(AccountingMovement.session_id == sid)
                )
            )
            .mappings()
            .one()
        )
        payload = latest.payload if latest else {}
        measured = await performance(
            db, sid, research_only=control is not None and control.classification == "RESEARCH"
        )
        summaries.append(
            {
                "session_id": str(sid),
                "name": session.name,
                "classification": control.classification if control else "UNAVAILABLE",
                "initial_equity_cad": exact(session.initial_equity_cad),
                "started_at": exact(session.started_at),
                "outcomes": outcomes,
                "accounting_known_subtotals": exact(dict(sums)),
                "unknown_movement_counts": dict(unknowns),
                "ledger_reconciliation": {
                    "unbalanced_journal_assets": unbalanced,
                    "negative_inventory_accounts": negative,
                    "balanced": unbalanced == 0 and negative == 0,
                },
                "latest_checkpoint_at": exact(latest.as_of) if latest else None,
                "latest_checkpoint": {
                    k: payload.get(k)
                    for k in (
                        "equity_cad",
                        "realized_trading_pnl_cad",
                        "realized_fx_pnl_cad",
                        "unrealized_pnl_cad",
                        "no_trading_benchmark_cad",
                        "strategy_excess_cad",
                        "reconciliation_delta_cad",
                        "fees_cad",
                        "expenses_cad",
                        "drawdown_pct",
                        "limitations",
                    )
                },
                "win_rate": measured.get("win_rate_pct"),
                "performance": measured,
                "win_rate_limitation": (
                    "Recalculate matched known-CAD completed trades from fills; "
                    "holding gains are not arbitrage profit."
                ),
            }
        )
    await result.close()
    return summaries


LIMITATIONS = [
    "All available RETAINED rows in one PostgreSQL repeatable-read "
    "snapshot; no date, asset, strategy or portfolio filter.",
    "Deleted/retention-expired observations cannot be recovered; their "
    "exact number is unknown. Retention events are included if retained.",
    "Transient collector memory, unsaved observations, host Docker metrics "
    "and proxy/application log files are not database history. Only "
    "retained telemetry is available.",
    "Current mutable registry, balances and statuses describe snapshot "
    "state; immutable events/evaluations establish historical decisions.",
    "Validated and research portfolios are independently funded. Never add "
    "their equity or hypothetical profits together.",
    "Unverified fees, eligibility, clock, depth or funding prevent "
    "actionability. Positive projected spread never proves executable real "
    "profit.",
    "Actual trades, if explicitly owner recorded, remain separate from all "
    "paper statistics. QUANTIX submits no real orders.",
    "Authentication, private Telegram identities/destinations/messages, "
    "personal identifiers and credential-bearing metadata are "
    "excluded/redacted intentionally.",
    "Null/empty values mean unavailable; measured zero remains zero. JSON "
    "financial values and SQL NUMERIC are exact decimal strings.",
    "All retained budget/size results are in evaluation/result evidence. "
    "Uncomputed budgets and missing post-delay books are not invented.",
]


