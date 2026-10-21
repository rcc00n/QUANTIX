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

    def _text(self, name, value):
        data = value.encode("utf-8")
        self.total_bytes += len(data)
        if self.total_bytes > MAX_UNCOMPRESSED_BYTES:
            raise HTTPException(413, "Export exceeds256MiB; shorten the reporting period")
        self.zip.writestr(name, data)
        self.files.append(
            {
                "name": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "records": None,
            }
        )

    async def text(self, name, value):
        await sequential_thread(self._text, name, value)

    async def json(self, name, value):
        def write():
            self._text(name, json.dumps(sanitize(value), ensure_ascii=False, indent=2))

        await sequential_thread(write)

    def close(self):
        self.zip.close()


async def records(db, query):
    result = await db.stream_scalars(query.execution_options(yield_per=BATCH_ROWS))
    async for row in result:
        yield record(row)


async def make_export(db, session, control, period, generated_at, clock, strategy_version_id=None):
    """Caller owns a REPEATABLE READ READ ONLY transaction and file cleanup on success."""
    handle, filename = tempfile.mkstemp(prefix="quantix-export-", suffix=".zip")
    os.close(handle)  # mkstemp is0600; artifacts are never placed under static/public.
    archive = Archive(filename)
    trade_where = [
        PaperTrade.session_id == session.id,
        PaperTrade.requested_at < period.end,
        PaperTrade.classification == control.classification,
    ]
    # Preserve all session dependencies needed to reconstruct moving cost basis.
    # Strategy selection scopes numerical metrics; CSV provenance labels membership.
    trade_ids = select(PaperTrade.id).where(*trade_where)
    evaluation_ids = select(ArbitrageEvaluation.id).where(
        or_(
            (ArbitrageEvaluation.evaluated_at >= period.start)
            & (ArbitrageEvaluation.evaluated_at < period.end),
            ArbitrageEvaluation.id.in_(select(PaperTrade.evaluation_id).where(*trade_where)),
        )
    )
    # All network observations are public; portfolio/accounting records remain owner-scoped.
    eval_where = [
        ArbitrageEvaluation.id.in_(evaluation_ids),
        ArbitrageEvaluation.evaluated_at < period.end,
    ]
    legacy_ids = select(Opportunity.id).where(
        or_(
            (Opportunity.detected_at >= period.start) & (Opportunity.detected_at < period.end),
            Opportunity.id.in_(select(PaperTrade.opportunity_id).where(*trade_where)),
        ),
        Opportunity.detected_at < period.end,
        Opportunity.strategy_version_id.in_(
            select(StrategyVersion.id).where(StrategyVersion.user_id == session.user_id)
        ),
    )
    signal_ids = select(Signal.id).where(
        Signal.opportunity_id.in_(legacy_ids),
        Signal.evaluated_at < period.end,
        Signal.strategy_version_id.in_(
            select(StrategyVersion.id).where(StrategyVersion.user_id == session.user_id)
        ),
    )
    alert_ids = select(AlertDecision.id).where(
        AlertDecision.user_id == session.user_id,
        AlertDecision.evaluation_id.in_(evaluation_ids),
        AlertDecision.evaluated_at < period.end,
    )
    fill_ids = (
        select(PaperFill.id)
        .join(PaperOrder)
        .where(PaperOrder.trade_id.in_(trade_ids), PaperFill.executed_at < period.end)
    )
    journal_ids = select(Journal.id).where(
        Journal.session_id == session.id, Journal.effective_at < period.end
    )
    account_ids = (
        select(VirtualAccount.id)
        .join(LedgerEntry)
        .join(Journal)
        .where(
            VirtualAccount.session_id == session.id,
            Journal.effective_at < period.end,
        )
        .distinct()
    )
    version_ids = (
        select(PaperTrade.strategy_version_id)
        .where(*trade_where)
        .union(
            select(StrategyVersion.id).where(
                StrategyVersion.id.in_([session.strategy_version_id, control.strategy_version_id])
            )
        )
    )
    snapshot_ids = (
        select(ArbitrageEvaluation.buy_snapshot_id)
        .where(*eval_where)
        .union(
            select(ArbitrageEvaluation.sell_snapshot_id).where(*eval_where),
            select(PaperFill.snapshot_id).where(PaperFill.id.in_(fill_ids)),
            select(ExecutionObservation.snapshot_id).where(
                ExecutionObservation.trade_id.in_(trade_ids),
                ExecutionObservation.received_at < period.end,
            ),
            select(Opportunity.buy_snapshot_id).where(Opportunity.id.in_(legacy_ids)),
            select(Opportunity.sell_snapshot_id).where(Opportunity.id.in_(legacy_ids)),
        )
    )
    fee_ids = (
        select(ArbitrageEvaluation.buy_fee_version_id)
        .where(*eval_where)
        .union(
            select(ArbitrageEvaluation.sell_fee_version_id).where(*eval_where),
            select(PaperFill.fee_version_id).where(PaperFill.id.in_(fill_ids)),
            select(Opportunity.buy_fee_version_id).where(Opportunity.id.in_(legacy_ids)),
            select(Opportunity.sell_fee_version_id).where(Opportunity.id.in_(legacy_ids)),
        )
    )
    fx_ids = (
        select(PaperFill.fx_quote_id)
        .where(PaperFill.id.in_(fill_ids))
        .union(
            select(Opportunity.fx_quote_id).where(Opportunity.id.in_(legacy_ids)),
            select(VirtualDeposit.fx_quote_id)
            .join(VirtualAccount)
            .join(Journal, Journal.id == VirtualDeposit.journal_id)
            .where(VirtualAccount.session_id == session.id, Journal.effective_at < period.end),
        )
    )
    market_ids = (
        select(OrderBookSnapshot.market_id)
        .where(OrderBookSnapshot.id.in_(snapshot_ids))
        .union(select(PaperOrder.market_id).where(PaperOrder.trade_id.in_(trade_ids)))
    )
    asset_ids = (
        select(VirtualAccount.asset_id)
        .where(VirtualAccount.id.in_(account_ids))
        .union(
            select(Market.base_asset_id).where(Market.id.in_(market_ids)),
            select(Market.quote_asset_id).where(Market.id.in_(market_ids)),
            select(PaperFill.fee_asset_id).where(PaperFill.id.in_(fill_ids)),
            select(FXQuote.base_asset_id).where(FXQuote.id.in_(fx_ids)),
            select(FXQuote.quote_asset_id).where(FXQuote.id.in_(fx_ids)),
        )
    )
    exchange_ids = (
        select(VirtualAccount.exchange_id)
        .where(VirtualAccount.id.in_(account_ids))
        .union(select(Market.exchange_id).where(Market.id.in_(market_ids)))
    )
    try:
        metrics = await analytics(db, session, control, period, strategy_version_id)
        metrics["generated_at"] = generated_at.isoformat()
        metrics["schema_version"] = "quantix-export-v1"
        metrics["clock"] = sanitize(clock)
        metrics["dataset_scope"] = (
            "Trade/ledger files include full-session opening/dependency history. "
            "Metrics use selected strategy and half-open period; CSV provenance labels membership. "
            "Network observations are separate."
        )
        metrics = AnalyticsExportMetrics.model_validate(metrics).model_dump(mode="json")

        async def opportunities():
            query = (
                select(
                    ArbitrageEvaluation,
                    ArbitrageRoute.buy_market_id,
                    ArbitrageRoute.sell_market_id,
                    ArbitrageRoute.detected_at,
                )
                .join(ArbitrageRoute)
                .where(*eval_where)
                .order_by(ArbitrageEvaluation.evaluated_at, ArbitrageEvaluation.id)
            )
            result = await db.stream(query.execution_options(yield_per=BATCH_ROWS))
            async for value, buy, sell, detected_at in result:
                yield {
                    "id": value.id,
                    "detected_at": detected_at,
                    "buy_market_id": buy,
                    "sell_market_id": sell,
                    "quantity": value.quantity,
                    "estimated_net_cad": value.net_profit_cad,
                    "calculation_version": value.calculation_version,
                    "provenance_json": {
                        "kind": "ARBITRAGE_EVALUATION",
                        "evaluation": record(value),
                        "in_reporting_period": period.start <= value.evaluated_at < period.end,
                    },
                }
            async for value in records(
                db,
                select(Opportunity)
                .where(Opportunity.id.in_(legacy_ids))
                .order_by(Opportunity.detected_at, Opportunity.id),
            ):
                yield {**value, "provenance_json": {"kind": "LEGACY_OPPORTUNITY", "record": value}}

        await archive.csv("opportunities.csv", CSV_COLUMNS["opportunities.csv"], opportunities())

        async def signals():
            delivered = (
                select(NotificationAttempt.id)
                .where(
                    NotificationAttempt.signal_id == Signal.id,
                    NotificationAttempt.channel == "telegram",
                    NotificationAttempt.status == "delivered",
                    NotificationAttempt.delivered_at < period.end,
                )
                .exists()
            )
            result = await db.stream(
                select(Signal, delivered)
                .where(Signal.id.in_(signal_ids))
                .order_by(Signal.evaluated_at, Signal.id)
                .execution_options(yield_per=BATCH_ROWS)
            )
            async for value, was_delivered in result:
                yield {
                    **record(value),
                    "telegram_delivery_status": "delivered"
                    if was_delivered
                    else "no_recorded_delivery",
                    "notification_attempts_json": {
                        "records_file": "notification_attempts.csv",
                        "join_key": "signal_id",
                    },
                }
            delivered_alert = (
                select(NotificationOutbox.id)
                .where(
                    NotificationOutbox.alert_id == AlertDecision.id,
                    NotificationOutbox.delivered_at < period.end,
                )
                .exists()
            )
            query = (
                select(AlertDecision, ArbitrageEvaluation.route_id, delivered_alert)
                .join(ArbitrageEvaluation, AlertDecision.evaluation_id == ArbitrageEvaluation.id)
                .where(AlertDecision.id.in_(alert_ids))
                .order_by(AlertDecision.evaluated_at, AlertDecision.id)
                .execution_options(yield_per=BATCH_ROWS)
            )
            async for decision, route_id, was_delivered in await db.stream(query):
                yield {
                    "id": decision.id,
                    "opportunity_id": route_id,
                    "evaluated_at": decision.evaluated_at,
                    "decision": decision.classification
                    + (":ELIGIBLE" if decision.eligible else ":REJECTED"),
                    "reason": decision.reasons,
                    "telegram_delivery_status": "delivered"
                    if was_delivered
                    else "no_recorded_delivery",
                    "notification_attempts_json": {
                        "records_file": "alert_outbox.csv",
                        "events_file": "alert_events.csv",
                        "join_key": "alert_id",
                        "scope": "owner_network_alerts",
                        "not_a_paper_execution": True,
                    },
                }

        await archive.csv("signals.csv", CSV_COLUMNS["signals.csv"], signals())

        async def trades():
            async for value in records(
                db,
                select(PaperTrade)
                .where(*trade_where)
                .order_by(PaperTrade.requested_at, PaperTrade.id),
            ):
                # Today's mutable state is not a historical observation.
                completed = value.get("completed_at")
                if completed is None or datetime.fromisoformat(completed) >= period.end:
                    value["status"] = "OPEN_AT_CUTOFF"
                    value["reason"] = None
                    value["completed_at"] = None
                    value["state"] = {
                        "limitation": "HISTORICAL_MUTABLE_STATE_UNAVAILABLE; see lifecycle.csv"
                    }
                yield {
                    **value,
                    "provenance_json": {
                        **value,
                        "selected_strategy_member": strategy_version_id is None
                        or value.get("strategy_version_id") == str(strategy_version_id),
                        "in_reporting_period": period.start
                        <= datetime.fromisoformat(value["requested_at"])
                        < period.end,
                    },
                }

        await archive.csv("paper_trades.csv", CSV_COLUMNS["paper_trades.csv"], trades())

        async def fills():
            query = (
                select(PaperFill, PaperOrder.trade_id, PaperOrder.side, Asset.symbol)
                .join(PaperOrder, PaperOrder.id == PaperFill.order_id)
                .join(Market, Market.id == PaperOrder.market_id)
                .join(Asset, Asset.id == Market.quote_asset_id)
                .where(PaperFill.id.in_(fill_ids))
                .order_by(PaperFill.executed_at, PaperFill.id)
            )
            result = await db.stream(query.execution_options(yield_per=BATCH_ROWS))
            async for value, trade_id, side, quote in result:
                yield {
                    **record(value),
                    "trade_id": trade_id,
                    "side": side,
                    "quote_asset": quote,
                    "fee_asset": str(value.fee_asset_id),
                    "provenance_json": record(value),
                }

        await archive.csv("paper_fills.csv", CSV_COLUMNS["paper_fills.csv"], fills())

        opening_id = (
            select(PortfolioCheckpoint.id)
            .where(
                PortfolioCheckpoint.session_id == session.id,
                PortfolioCheckpoint.as_of < period.start,
            )
            .order_by(PortfolioCheckpoint.as_of.desc(), PortfolioCheckpoint.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        equity_query = (
            select(PortfolioCheckpoint)
            .where(
                PortfolioCheckpoint.session_id == session.id,
                PortfolioCheckpoint.as_of < period.end,
                or_(
                    PortfolioCheckpoint.as_of >= period.start, PortfolioCheckpoint.id == opening_id
                ),
            )
            .order_by(PortfolioCheckpoint.as_of, PortfolioCheckpoint.id)
        )

        async def equity():
            result = await db.stream_scalars(equity_query.execution_options(yield_per=BATCH_ROWS))
            async for value in result:
                yield {
                    **value.payload,
                    "session_id": session.id,
                    "as_of": value.as_of,
                    "net_external_flows_cad": value.payload.get("funded_capital_cad"),
                    "valuation_inputs_json": record(value),
                }

        await archive.csv("portfolio_equity.csv", CSV_COLUMNS["portfolio_equity.csv"], equity())

        async def balances():
            query = (
                select(VirtualAccount, Asset.symbol, Exchange.slug, func.sum(LedgerEntry.quantity))
                .join(Asset, Asset.id == VirtualAccount.asset_id)
                .outerjoin(Exchange, Exchange.id == VirtualAccount.exchange_id)
                .join(LedgerEntry, LedgerEntry.account_id == VirtualAccount.id)
                .join(Journal, Journal.id == LedgerEntry.journal_id)
                .where(Journal.id.in_(journal_ids))
                .group_by(VirtualAccount.id, Asset.symbol, Exchange.slug)
                .order_by(VirtualAccount.id)
            )
            result = await db.stream(query.execution_options(yield_per=BATCH_ROWS))
            async for account, asset, exchange, quantity in result:
                yield {
                    "session_id": session.id,
                    "exchange": exchange,
                    "asset": asset,
                    "account_role": account.role,
                    "quantity": quantity,
                    "as_of": period.end,
                    "ledger_entries_json": {
                        "account": record(account),
                        "records_file": "ledger_entries.csv",
                        "join_key": "account_id",
                        "cutoff_is_exclusive": True,
                    },
                }

        await archive.csv("balances.csv", CSV_COLUMNS["balances.csv"], balances())

        async def fees():
            query = (
                select(PaperFill, PaperOrder.trade_id)
                .join(PaperOrder)
                .where(PaperFill.id.in_(fill_ids))
                .order_by(PaperFill.executed_at, PaperFill.id)
            )
            result = await db.stream(query.execution_options(yield_per=BATCH_ROWS))
            async for value, trade_id in result:
                yield {
                    "id": value.id,
                    "trade_id": trade_id,
                    "category": "trading",
                    "quantity": value.fee_quantity,
                    "asset": value.fee_asset_id,
                    "cad_value": value.fee_cad,
                    "effective_at": value.executed_at,
                    "fee_version_id": value.fee_version_id,
                    "fx_quote_id": value.fx_quote_id,
                    "assumptions_json": value.provenance,
                }
            async for value in records(
                db,
                select(InventoryTransfer)
                .where(
                    InventoryTransfer.session_id == session.id,
                    InventoryTransfer.settled_at < period.end,
                )
                .order_by(InventoryTransfer.settled_at, InventoryTransfer.id),
            ):
                yield {
                    "id": value["id"],
                    "trade_id": None,
                    "category": "transfer_rebalancing",
                    "quantity": value["expense_quantity"],
                    "asset": value["expense_asset_id"],
                    "cad_value": value["expense_cad"],
                    "effective_at": value["settled_at"],
                    "assumptions_json": value,
                }

        await archive.csv("fees.csv", CSV_COLUMNS["fees.csv"], fees())

        async def health():
            query = (
                select(SystemEvent, Exchange.slug)
                .outerjoin(Exchange)
                .where(
                    SystemEvent.created_at >= period.start,
                    SystemEvent.created_at < period.end,
                    or_(
                        SystemEvent.actor_user_id.is_(None),
                        SystemEvent.actor_user_id == session.user_id,
                    ),
                )
                .order_by(SystemEvent.created_at, SystemEvent.id)
            )
            result = await db.stream(query.execution_options(yield_per=BATCH_ROWS))
            async for value, exchange in result:
                yield {**record(value), "exchange": exchange, "details_json": value.details}

        await archive.csv("system_health.csv", CSV_COLUMNS["system_health.csv"], health())

        async def raw(query):
            async for value in records(db, query):
                yield {**value, "record_json": value}

        async def orders():
            quantity = func.coalesce(
                func.sum(case((PaperFill.executed_at < period.end, PaperFill.quantity), else_=0)), 0
            )
            query = (
                select(PaperOrder, quantity)
                .outerjoin(PaperFill)
                .where(PaperOrder.trade_id.in_(trade_ids))
                .group_by(PaperOrder.id)
                .order_by(PaperOrder.trade_id, PaperOrder.id)
            )
            result = await db.stream(query.execution_options(yield_per=BATCH_ROWS))
            async for order, filled in result:
                value = record(order)
                value["status"] = (
                    "filled"
                    if filled >= order.quantity
                    else ("partial" if filled > 0 else "NO_RECORDED_FILL_BEFORE_CUTOFF")
                )
                value["reason"] = None
                yield {
                    **value,
                    "record_json": {
                        **value,
                        "filled_before_cutoff": str(filled),
                        "status_definition": "Recorded fills only; later mutable status excluded",
                    },
                }

        await archive.csv(
            "orders.csv",
            tuple(c.name for c in PaperOrder.__table__.columns) + ("record_json",),
            orders(),
        )

        async def attempts():
            async for value in records(
                db,
                select(NotificationAttempt)
                .where(
                    NotificationAttempt.signal_id.in_(signal_ids),
                    NotificationAttempt.created_at < period.end,
                )
                .order_by(NotificationAttempt.created_at, NotificationAttempt.id),
            ):
                delivered = value.get("delivered_at")
                if delivered is None or datetime.fromisoformat(delivered) >= period.end:
                    value.update(
                        status="UNDETERMINED_AT_CUTOFF", delivered_at=None, error_code=None
                    )
                value.pop("provider_message_id", None)
                yield {**value, "record_json": value}

        await archive.csv(
            "notification_attempts.csv",
            (
                "id",
                "signal_id",
                "channel",
                "attempt",
                "status",
                "created_at",
                "delivered_at",
                "error_code",
                "record_json",
            ),
            attempts(),
        )

        async def alert_outbox():
            async for row in records(
                db,
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.alert_id.in_(alert_ids),
                    NotificationOutbox.created_at < period.end,
                )
                .order_by(NotificationOutbox.created_at, NotificationOutbox.id),
            ):
                for private in ("binding_id", "provider_message_id", "message"):
                    row.pop(private, None)
                delivered = row.get("delivered_at")
                if delivered is None or datetime.fromisoformat(delivered) >= period.end:
                    row.update(
                        status="UNDETERMINED_AT_CUTOFF",
                        delivered_at=None,
                        attempts=None,
                        error_code=None,
                        available_at=None,
                    )
                yield {**row, "record_json": row}

        await archive.csv(
            "alert_outbox.csv",
            (
                "id",
                "alert_id",
                "kind",
                "status",
                "created_at",
                "available_at",
                "expires_at",
                "delivered_at",
                "attempts",
                "error_code",
                "record_json",
            ),
            alert_outbox(),
        )

        extra_queries = {
            "alert_decisions.csv": (
                AlertDecision,
                select(AlertDecision)
                .where(AlertDecision.id.in_(alert_ids))
                .order_by(AlertDecision.evaluated_at, AlertDecision.id),
            ),
            "alert_events.csv": (
                NotificationEvent,
                select(NotificationEvent)
                .where(
                    NotificationEvent.user_id == session.user_id,
                    NotificationEvent.alert_id.in_(alert_ids),
                    NotificationEvent.created_at < period.end,
                )
                .order_by(NotificationEvent.created_at, NotificationEvent.id),
            ),
            "terminal_settings_history.csv": (
                TerminalSettingsVersion,
                select(TerminalSettingsVersion)
                .where(
                    TerminalSettingsVersion.user_id == session.user_id,
                    TerminalSettingsVersion.effective_at < period.end,
                )
                .order_by(TerminalSettingsVersion.effective_at, TerminalSettingsVersion.version),
            ),
            "journals.csv": (
                Journal,
                select(Journal)
                .where(Journal.id.in_(journal_ids))
                .order_by(Journal.effective_at, Journal.id),
            ),
            "ledger_entries.csv": (
                LedgerEntry,
                select(LedgerEntry)
                .where(LedgerEntry.journal_id.in_(journal_ids))
                .order_by(LedgerEntry.journal_id, LedgerEntry.id),
            ),
            "accounts.csv": (
                VirtualAccount,
                select(VirtualAccount)
                .where(VirtualAccount.id.in_(account_ids))
                .order_by(VirtualAccount.id),
            ),
            "accounting_movements.csv": (
                AccountingMovement,
                select(AccountingMovement)
                .where(
                    AccountingMovement.session_id == session.id,
                    AccountingMovement.effective_at < period.end,
                )
                .order_by(AccountingMovement.effective_at, AccountingMovement.id),
            ),
            "lifecycle.csv": (
                SimulationEvent,
                select(SimulationEvent)
                .where(
                    SimulationEvent.trade_id.in_(trade_ids),
                    SimulationEvent.occurred_at < period.end,
                )
                .order_by(
                    SimulationEvent.occurred_at,
                    lifecycle_rank(SimulationEvent.event_type),
                    SimulationEvent.id,
                ),
            ),
            "execution_observations.csv": (
                ExecutionObservation,
                select(ExecutionObservation)
                .where(
                    ExecutionObservation.trade_id.in_(trade_ids),
                    ExecutionObservation.received_at < period.end,
                )
                .order_by(ExecutionObservation.received_at, ExecutionObservation.id),
            ),
            "order_books.csv": (
                OrderBookSnapshot,
                select(OrderBookSnapshot)
                .where(OrderBookSnapshot.id.in_(snapshot_ids))
                .order_by(OrderBookSnapshot.received_at, OrderBookSnapshot.id),
            ),
            "fee_versions.csv": (
                FeeVersion,
                select(FeeVersion)
                .where(FeeVersion.id.in_(fee_ids))
                .order_by(FeeVersion.effective_at, FeeVersion.id),
            ),
            "fx_quotes.csv": (
                FXQuote,
                select(FXQuote)
                .where(FXQuote.id.in_(fx_ids))
                .order_by(FXQuote.observed_at, FXQuote.id),
            ),
            "markets.csv": (
                Market,
                select(Market).where(Market.id.in_(market_ids)).order_by(Market.id),
            ),
            "assets.csv": (Asset, select(Asset).where(Asset.id.in_(asset_ids)).order_by(Asset.id)),
            "exchanges.csv": (
                Exchange,
                select(Exchange).where(Exchange.id.in_(exchange_ids)).order_by(Exchange.slug),
            ),
            "strategy_history.csv": (
                StrategyVersion,
                select(StrategyVersion)
                .where(
                    StrategyVersion.id.in_(version_ids), StrategyVersion.user_id == session.user_id
                )
                .order_by(StrategyVersion.version),
            ),
            "opportunity_invalidations.csv": (
                ArbitrageLifecycleEvent,
                select(ArbitrageLifecycleEvent)
                .where(
                    ArbitrageLifecycleEvent.route_id.in_(
                        select(ArbitrageEvaluation.route_id).where(*eval_where)
                    ),
                    ArbitrageLifecycleEvent.event_at >= period.start,
                    ArbitrageLifecycleEvent.event_at < period.end,
                )
                .order_by(ArbitrageLifecycleEvent.event_at, ArbitrageLifecycleEvent.id),
            ),
        }
        for name, (model, query) in extra_queries.items():
            columns = tuple(c.name for c in model.__table__.columns) + ("record_json",)
            await archive.csv(name, columns, raw(query))

        async def benchmarks():
            async for row in equity():
                yield {
                    "session_id": session.id,
                    "as_of": row["as_of"],
                    "benchmark_cad": row.get("no_trading_benchmark_cad"),
                    "equity_cad": row.get("equity_cad"),
                    "method": "Original funded units; contemporaneous observed-depth CAD marks",
                }

        await archive.csv(
            "benchmark.csv",
            ("session_id", "as_of", "benchmark_cad", "equity_cad", "method"),
            benchmarks(),
        )

        async def scenarios():
            if control.classification == "RESEARCH":
                yield {
                    "session_id": session.id,
                    "classification": control.classification,
                    "name": session.name,
                    "initial_equity_cad": session.initial_equity_cad,
                    "benchmark_json": session.benchmark_snapshot,
                }

        await archive.csv(
            "research_scenarios.csv",
            ("session_id", "classification", "name", "initial_equity_cad", "benchmark_json"),
            scenarios(),
        )
        terminal_at_cutoff = await db.scalar(
            select(TerminalSettingsVersion)
            .where(
                TerminalSettingsVersion.user_id == session.user_id,
                TerminalSettingsVersion.effective_at < period.end,
            )
            .order_by(
                TerminalSettingsVersion.version.desc(), TerminalSettingsVersion.effective_at.desc()
            )
            .limit(1)
        )
        config = {
            "schema_version": "quantix-export-v1",
            "paper_only": True,
            "session": record(session),
            "classification": control.classification,
            "reporting_period": period.model_dump(mode="json"),
            "selected_strategy_version_id": str(strategy_version_id)
            if strategy_version_id
            else None,
            "current_strategy_version_id": str(control.strategy_version_id),
            "initial_conditions": session.benchmark_snapshot,
            "terminal_preferences_at_cutoff": record(terminal_at_cutoff)
            if terminal_at_cutoff
            else None,
            "terminal_preferences_limitation": None
            if terminal_at_cutoff
            else (
                "No persisted terminal preferences effective before cutoff; later settings excluded"
            ),
            "notification_delivery_active": False
            if terminal_at_cutoff
            and terminal_at_cutoff.settings.get("notifications", {}).get("telegram_enabled")
            is False
            else None,
            "notification_delivery_limitation": (
                "Explicit disabled preferences at cutoff mean alert delivery inactive. "
                "Other historical runtime activity is not inferred from current pairing. "
                "Use immutable alert_events and delivered_at evidence before cutoff."
            ),
            "notification_scope": (
                "Owner network alerts for retained evaluations, "
                "distinct from this paper portfolio; "
                "private commands, pairing and destination identifiers are excluded."
            ),
            "definitions": {
                "currency": "CAD",
                "decimal": "NUMERIC(38,18), base-10 strings",
                "timezone": "America/Edmonton daily periods, stored UTC",
                "period": "[start,end)",
                "cost_basis": "Per-exchange/asset moving weighted average CAD",
                "fees": "Already reflected in cashflow/basis; do not subtract twice",
                "research": "Isolated hypothetical capital/results, never verified execution",
                "portfolio_strategy_filter": (
                    "Whole-session equity/accounting; strategy filters trade results only"
                ),
            },
            "provenance_files": list(extra_queries),
            "calculation_versions": {
                "current_depth_engine": CALCULATION_VERSION,
                "current_accounting": ACCOUNTING_VERSION,
                "original": "Original versions remain in every evaluation/trade/checkpoint",
            },
            "clock": clock,
            "limitations": metrics["limitations"],
            "external_ai_used": False,
        }
        await archive.json("config_snapshot.json", config)
        await archive.json("metrics.json", metrics)
        counts = metrics["metrics"]
        win_rate = counts["win_rate_pct"]
        summary = (
            f"# QUANTIX paper analysis\n\nPeriod: {period.start.isoformat()} ≤ t < "
            f"{period.end.isoformat()} UTC.\n"
            f"Generated: {generated_at.isoformat()}. Session: {session.id}. "
            f"Classification: {control.classification}.\n\n"
            f"Initial capital: {session.initial_equity_cad} CAD. "
            f"Completed cohort: {counts['completed_simulations']}; "
            f"partial: {counts['partially_completed']}; missed: {counts['missed_simulations']}; "
            f"failed: {counts['failed_simulations']}; expired: {counts['expired_simulations']}.\n"
            f"Win rate: {win_rate if win_rate is not None else 'unavailable'}%. "
            f"Ledger balanced: {metrics['reconciliation']['balanced']}.\n\n"
            "All trades are simulated. Research is hypothetical and excluded "
            "from validated portfolios. "
            "No AI conclusions, fabricated rows or actual exchange executions are included. "
            "Zero completed trades means there is no measured completed-trade win rate.\n\n"
            "Trade and ledger datasets include opening history before the period "
            "to reconstruct cost basis. "
            "Period membership is determined by the documented timestamp in each "
            "row, never by row count. "
            "Metrics use requested-at cohorts, completed-at trade cashflows and "
            "effective-at journals separately. "
            "Network opportunity detections are public scanner activity, not "
            "attributed strategy performance.\n\n"
            "Fees already affect cashflows/basis. Compare strategy excess "
            "against original-holdings benchmark; "
            "holding gains are not arbitrage profit. Missing rates/valuations remain null. "
            "Retained equity checkpoints may predate the cutoff and are not a "
            "live price guarantee.\n\n"
            "Limitations:\n" + "\n".join("- " + str(x) for x in metrics["limitations"]) + "\n"
        )
        await archive.text("SUMMARY.md", summary)
        readme = (
            "# QUANTIX analytical export\n\nEvery CSV is UTF-8 RFC4180 with a "
            "header even when empty. "
            "JSON decimals are exact base-10 strings; null/empty means unavailable, not zero. "
            "Percent means percentage points, fee rates fractions. Timestamps are ISO8601 UTC. "
            "Daily timezone is America/Edmonton. No financial interpolation occurs.\n\n"
            "Mandatory files retain the v1 ordered headers. Additional raw CSVs "
            "make all referenced "
            "books, fees, FX, orders, lifecycle, journals and entries "
            "inspectable without DB access. "
            "Join by UUID. record_json preserves complete sanitized source fields. "
            "balances.csv is ledger-derived at the exclusive cutoff, including "
            "system counteraccounts; "
            "only available/reserved roles are exchange inventory. Opening history is included. "
            "Strategy-filtered exports preserve whole-session accounting needed "
            "to reconcile inventory.\n\n"
            "Historical mutable trade/order state may not be reconstructible; use lifecycle/fill "
            "timestamps and explicit limitations, never a later result for an earlier decision. "
            "Snapshots preserve original availability/source time, uncertainty and confidence. "
            "Markets/exchanges metadata represents retained current registry "
            "metadata, not a historical "
            "eligibility approval; exact historical calculation inputs remain in evidence. "
            "Non-numeric spreadsheet formula prefixes are escaped; original "
            "values remain in record_json.\n\n"
            "Column dictionary:\n\n"
            + "\n\n".join(
                f"### {f['name']}\n\n"
                + "\n".join(f"- `{column}`: {column_definition(column)}" for column in f["columns"])
                for f in archive.files
                if f.get("columns")
            )
            + "\n"
        )
        readme += (
            "\n\n## Reading wide evidence fields in Python\n\n"
            "Full provenance fields can legitimately exceed Python csv's default128KiB. "
            "Use the archive's documented256MiB maximum as a parser bound and stream rows:\n\n"
            "```python\nimport csv, io, zipfile\ncsv.field_size_limit(256 * 1024 * 1024)\n"
            "with zipfile.ZipFile('quantix-analysis.zip') as archive:\n"
            "    with archive.open('opportunities.csv') as member:\n"
            "        text = io.TextIOWrapper(member, encoding='utf-8', newline='')\n"
            "        for row in csv.DictReader(text):\n"
            "            pass  # inspect one genuine record at a time\n```\n"
        )
        from arbiq.market_data.timing import TIMING_EXPORT_NOTES

        await archive.text("README.md", readme + TIMING_EXPORT_NOTES)
        manifest = {
            "schema_version": "quantix-export-v1",
            "paper_only": True,
            "generated_at": generated_at.isoformat(),
            "period": period.model_dump(mode="json"),
            "session_id": str(session.id),
            "classification": control.classification,
            "strategy_version_id": str(strategy_version_id) if strategy_version_id else None,
            "required_files": list(EXPORT_FILES),
            "files": archive.files,
            "hash_algorithm": "SHA-256 of uncompressed bytes; manifest excludes its own hash",
            "limits": {
                "rows_per_file": MAX_ROWS_PER_FILE,
                "uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
            },
            "reconciliation": metrics["reconciliation"],
            "complete": True,
        }
        manifest = AnalyticsExportManifest.model_validate(manifest).model_dump(
            mode="json", exclude_none=True
        )
        await archive.json("export_manifest.json", manifest)
        await sequential_thread(archive.close)
        return Path(filename), manifest
    except BaseException:
        archive.close()
        await asyncio.to_thread(Path(filename).unlink, missing_ok=True)
        raise
