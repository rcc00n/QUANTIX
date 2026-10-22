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


async def _build_full_attempt(db, owner, path, progress, clock, *, analysis=False):
    unknown = set(Base.metadata.tables) - TABLE_GROUP.keys() - EXCLUDED_TABLES.keys()
    if unknown:
        raise RuntimeError(
            "UNCLASSIFIED_SCHEMA: export policy needed for " + ",".join(sorted(unknown))
        )
    snapshot_time = await db.scalar(select(func.clock_timestamp()))
    snapshot = await db.scalar(text("SELECT txid_current_snapshot()::text"))
    portfolios = await portfolio_metrics(db, owner)
    archive = FullArchive(path, progress, analysis=analysis)
    dictionary = [
        "# Data dictionary\n",
        "Exact decimals: base10 strings; dates: UTC ISO8601; percentages: "
        "percentage points; fees: fractions. Empty CSV cells mean SQL NULL; "
        "JSON empty strings remain strings in JSON fields. CSV formula-leading "
        "text is escaped with a single quote.\n",
        "Join UUID primary/foreign keys. user IDs and private notification "
        "bindings are deliberately removed. Primary-key ordering is not causal "
        "ordering: use source/ingestion/availability timestamps.\n",
    ]
    try:
        if analysis:
            # Preserve complete evidence for every owned paper/alert-linked evaluation.
            from arbiq.db.models import PaperSession, PaperTrade
            from arbiq.notifications.models import AlertDecision

            important = (
                select(PaperTrade.evaluation_id)
                .join(PaperSession)
                .where(PaperSession.user_id == owner)
                .union(select(AlertDecision.evaluation_id).where(AlertDecision.user_id == owner))
            )
            result = await db.stream_scalars(important.execution_options(yield_per=BATCH))
            async for identifier in result:
                if identifier is not None:
                    await sequential_thread(archive.selection.important, identifier)
            await result.close()
        for name, group in TABLE_GROUP.items():
            table = Base.metadata.tables[name]
            columns, query, owned = dataset_query(table, owner)
            expected_count = await db.scalar(
                select(func.count()).select_from(query.order_by(None).subquery())
            )
            removed = [c.name for c in table.c if c.name in PRIVATE_COLUMNS]
            entry = await archive.csv(
                f"{group}/{name}.csv",
                [c.name for c in columns],
                stream_rows(db, query),
                source_table=name,
                scope="authenticated owner and dependencies"
                if owned
                else "shared public market/system evidence",
                excluded_columns=removed,
                source_snapshot_rows=expected_count,
                json_columns=[c.name for c in columns if isinstance(c.type, JSON)],
            )
            if entry["rows"] != expected_count:
                raise RuntimeError("ROW_COUNT_MISMATCH: complete snapshot cannot be published")
            dictionary.append(
                f"\n## {group}/{name}.csv\n{entry['rows']} retained rows; {entry['scope']}.\n"
            )
            for c in columns:
                references = ", ".join(fk.target_fullname for fk in c.foreign_keys)
                dictionary.append(
                    f"- `{c.name}`: {c.type}; nullable={c.nullable}"
                    + (f"; references {references}" if references else "")
                    + ".\n"
                )
        counts = {f["source_table"]: f["rows"] for f in archive.files if "source_table" in f}
        metrics = {
            "schema_version": SCHEMA,
            "coverage_level": "analysis_snapshot" if analysis else "lossless_retained_history",
            "lossless_raw_history": not analysis,
            "snapshot_at": exact(snapshot_time),
            "paper_only": True,
            "scope": "all retained owner portfolios and shared market evidence",
            "counts": counts,
            "portfolios": portfolios,
            "combined_portfolio_equity": None,
            "limitations": LIMITATIONS,
            "clock": clock,
        }
        await progress("Packing shared evidence", 0, archive.bytes)
        for view_name, columns in (
            ("opportunities/analysis.csv", OPPORTUNITY_COLUMNS),
            ("analytics/portfolio_history.csv", PORTFOLIO_COLUMNS),
        ):

            def iterator(name=view_name):
                return archive.store.projected_rows(name)

            # Fetch one bounded projection batch off the event loop; SQLite is scratch only.
            source = iterator()

            async def projected(source=source):
                while True:
                    batch = await sequential_thread(lambda: list(itertools.islice(source, BATCH)))
                    if not batch:
                        break
                    for row in batch:
                        yield row

            await archive.csv(
                view_name,
                columns,
                projected(),
                auxiliary=True,
                derived_from="all retained source rows",
                scope="readable financial projection",
            )
        await write_coverage(db, archive, snapshot_time)
        await archive.evidence()
        await archive.text(
            "load_export.py", Path(__file__).with_name("export_loader.py").read_text()
        )
        await archive.json("analytics/metrics.json", metrics)
        await archive.text(
            "START_HERE.md",
            "# QUANTIX — Export Everything for ChatGPT\n\n"
            + (
                "ANALYSIS SNAPSHOT: raw payload reductions are explicit in manifest.json. "
                if analysis
                else "LOSSLESS RETAINED HISTORY: shared evidence is exactly reconstructable. "
            )
            + "Not a profitability guarantee. Start with "
            "SUMMARY.md, analytics/metrics.json and manifest.json. "
            "Compare independently funded VALIDATED and RESEARCH portfolios "
            "separately. Inspect evaluation inputs/results, actual bid/ask depth, "
            "costs, clock, eligibility, "
            "post-delay execution evidence and ledger before concluding any "
            "arbitrage is executable.\n\n"
            "Coverage: all retained dates, portfolios, strategies, assets and "
            "exchanges. Empty datasets have headers and explicit manifest entries. "
            "Unavailable/deleted data is not invented. All limitations appear in "
            "manifest.json.\n",
        )
        lines = [
            "# QUANTIX numerical summary\n",
            "ANALYSIS SNAPSHOT: raw reductions are disclosed in manifest.json.\n"
            if analysis
            else "LOSSLESS RETAINED HISTORY: shared evidence is fully reconstructable.\n",
            f"Snapshot UTC: {snapshot_time.isoformat()}; envelope: {SCHEMA}.\n",
            f"Portfolios: {len(portfolios)}; routes: {counts.get('arbitrage_routes')}; "
            f"evaluations: {counts.get('arbitrage_evaluations')}; "
            f"paper attempts: {counts.get('paper_trades')}; "
            f"paper fills: {counts.get('paper_fills')}.\n",
            "No aggregate equity/profit is produced across independent capital. "
            "Known movement subtotals are incomplete wherever unknown counts are "
            "nonzero.\n",
        ]
        for p in portfolios:
            lines.append(
                f"\n- Portfolio `{p['session_id']}` ({p['classification']}), "
                f"starting CAD {p['initial_equity_cad']}; "
                f"outcomes {json.dumps(p['outcomes'], sort_keys=True)}; "
                f"ledger balanced={p['ledger_reconciliation']['balanced']}. "
                "Equity, benchmark and movement subtotals: analytics/metrics.json; "
                "complete historical checkpoints and journals: analytics/ and "
                "executions/.\n"
            )
            checkpoint = p["latest_checkpoint"]
            lines.append(
                f"  Latest retained equity CAD: {checkpoint.get('equity_cad')}; "
                f"realized disposal P&L: {checkpoint.get('realized_trading_pnl_cad')}; "
                f"unrealized inventory P&L: {checkpoint.get('unrealized_pnl_cad')}; "
                f"no-trading benchmark: {checkpoint.get('no_trading_benchmark_cad')}; "
                f"strategy excess: {checkpoint.get('strategy_excess_cad')}. "
                "None means unavailable, never zero.\n"
            )
        lines += ["\n## Limitations\n"] + [f"- {item}\n" for item in LIMITATIONS]
        await archive.text("SUMMARY.md", "".join(lines))
        await archive.text("data_dictionary.md", "".join(dictionary))
        await archive.text(
            "README.md",
            "# Independent analysis\n\n"
            "Start with analytics/metrics.json, opportunities/analysis.csv, "
            "analytics/portfolio_history.csv and exchanges/market_coverage.csv. "
            "These are readable without the shared-evidence decoder. For detailed "
            "JSON cells use load_export.py and EVIDENCE_FORMAT.md.\n\n"
            "1. Verify checksums.sha256 with `sha256sum -c checksums.sha256` after "
            "extracting into a private directory.\n"
            "2. Read START_HERE.md, SUMMARY.md, manifest.json and "
            "data_dictionary.md. Do not infer missing data as zero.\n"
            "3. Use Python Decimal (not float) for financial fields; CSV JSON cells "
            "preserve source records. Set `csv.field_size_limit(256*1024*1024)` for "
            "wide retained evidence.\n"
            "4. Join paper_sessions to paper_controls, trades/orders/fills and "
            "immutable journals/ledger_entries/virtual_accounts. Sum journal "
            "quantities per asset to zero; sum account quantities for balances.\n"
            "5. Compare equity and initial-holdings benchmarks at the same retained "
            "checkpoint. Fees already affect cashflows/basis; do not subtract "
            "twice. Separate holding gains from matched-leg arbitrage.\n"
            "6. Join evaluations/routes to markets/exchanges/assets, snapshots, fee "
            "versions and FX. Review all reasons, lifecycle events, depth, budgets "
            "and required sell inventory. Never assume stablecoin parity or "
            "Canadian eligibility.\n"
            "7. Review full market universe and actually monitored books "
            "separately, SOL/ETH/BTC and other assets via markets→assets. Freshness "
            "and latency need timestamp and health evidence.\n"
            "8. Alert decisions, lifecycle, outbox status/attempts/events represent "
            "actual retained delivery history; message bodies/destinations are "
            "intentionally absent.\n"
            "9. Prioritize findings with exact source IDs/timestamps and "
            "confidence; state evidence gaps. No external AI conclusion is "
            "included.\n\n"
            "manifest.json lists every member except itself and checksums.sha256 to "
            "avoid circular hashes. checksums.sha256 covers every other file "
            "including manifest.json. "
            "Schema-column decimal strings, record counts and timestamp ranges "
            "support independent reconciliation. No record-count/date window cap is "
            "applied.\n" + TIMING_EXPORT_NOTES,
        )
        await archive.text(
            "EVIDENCE_FORMAT.md",
            (
                "# Lossless shared evidence\n\n"
                "Selected large JSON cells use quantix-json-dag-v1 envelopes. "
                "evidence/objects.jsonl contains [id, typed node]. Its full member SHA256 "
                "and each CSV's decoded JSON semantic SHA256 validate all content. "
                "m is a source map, a is an array, r is a shared node reference; all original "
                "containers are typed, so literal source keys cannot collide with markers. "
                "Financial decimals remain original strings. No numeric precision is lost.\n\n"
                "Use only Python standard library: `from load_export import ExportReader`; "
                "`reader=ExportReader('archive.zip')`; `for row in reader.rows('opportunities/"
                "arbitrage_evaluations.csv'): ...`; `reader.close()`. JSON columns decode "
                "automatically. The disk-backed index uses bounded memory. To recover readable "
                "CSV: `python load_export.py archive.zip --dataset NAME --output restored.csv`.\n\n"
                "Every CSV lists json_columns and a canonical decoded JSON-cell semantic SHA256 "
                "(one compact sorted-key JSON object plus LF per row). Essential financial tables "
                "remain plain CSV/JSON; analytics metrics are readable without decoding. "
                "In analysis fallback, omitted raw wrappers carry original hashes; stratified "
                "samples keep the lowest-PK row per UTC hour/market/state/code plus unusual "
                "divergence/crossed records. All row scalars, accounting, decision outcomes, "
                "calculation inputs and execution/order-book evidence remain; "
                "PostgreSQL is unchanged.\n"
            ),
        )
        manifest = {
            "schema_version": SCHEMA,
            "complete": True,
            "coverage_level": "analysis_snapshot" if analysis else "lossless_retained_history",
            "lossless_raw_history": not analysis,
            "final_zip_limit_bytes": MAX_ZIP_BYTES,
            "codec": CODEC,
            "deduplication": {
                "unique_nodes": archive.store.nodes,
                "reused_nodes": archive.store.hits,
            },
            "raw_payload_reduced_rows": sum(
                f.get("raw_payload_reduced_rows", 0) for f in archive.files
            ),
            "source_rows_omitted": 0,
            "sampling_method": (
                "lowest PK per UTC-hour/market/state/code plus extreme/crossed/divergence"
            )
            if analysis
            else None,
            "intermediate_disk_peak_bytes": archive.temp_peak,
            "snapshot_at": exact(snapshot_time),
            "database_snapshot": snapshot,
            "scope": "ALL_RETAINED",
            "filters": None,
            "portfolio_ids": [p["session_id"] for p in portfolios],
            "files": list(archive.files),
            "counts": counts,
            "total_dataset_rows": sum(counts.values()),
            "limitations": LIMITATIONS,
            "excluded_sources": EXCLUDED_TABLES,
            "clock": clock,
            "financial_reconciliation": [
                dict(session_id=p["session_id"], **p["ledger_reconciliation"]) for p in portfolios
            ],
            "null_convention": "empty CSV / JSON null = unavailable; never replace with zero",
            "retention_expired_count": None,
            "retention_expired_reason": (
                "Unrecoverable deleted rows are not counted by current storage."
            ),
            "integrity": "manifest lists all preceding members; checksums.sha256 covers all "
            "members except itself, including manifest.json",
        }
        await archive.json("manifest.json", manifest)
        await archive.text(
            "checksums.sha256", "".join(f"{f['sha256']}  {f['name']}\n" for f in archive.files)
        )
        return manifest
    finally:
        await sequential_thread(archive.close)
        if "manifest" in locals():
            manifest["intermediate_disk_peak_bytes"] = archive.temp_peak


