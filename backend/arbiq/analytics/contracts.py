"""Versioned reporting interface. Stage 5 supplies actual aggregation and ZIP export."""

from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from arbiq.finance.contracts import (
    Contract,
    ExactDecimal,
    NonNegativeDecimal,
    UTCTimestamp,
)

REPORT_SCHEMA_VERSION = "1.0"

# Stable ordered CSV headers; IDs join across files. Decimal values are strings.
CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "opportunities.csv": (
        "id",
        "detected_at",
        "buy_market_id",
        "sell_market_id",
        "quantity",
        "estimated_net_cad",
        "calculation_version",
        "provenance_json",
    ),
    "signals.csv": (
        "id",
        "opportunity_id",
        "evaluated_at",
        "decision",
        "reason",
        "telegram_delivery_status",
        "notification_attempts_json",
    ),
    "paper_trades.csv": (
        "id",
        "session_id",
        "opportunity_id",
        "status",
        "reason",
        "requested_at",
        "eligible_at",
        "completed_at",
        "reaction_delay_ms",
        "provenance_json",
    ),
    "paper_fills.csv": (
        "id",
        "order_id",
        "trade_id",
        "executed_at",
        "side",
        "quantity",
        "price",
        "quote_asset",
        "fee_quantity",
        "fee_asset",
        "fee_cad",
        "cost_basis_cad",
        "realized_simulated_pnl_cad",
        "provenance_json",
    ),
    "portfolio_equity.csv": (
        "session_id",
        "as_of",
        "equity_cad",
        "realized_simulated_pnl_cad",
        "unrealized_pnl_cad",
        "capital_utilization_pct",
        "drawdown_pct",
        "initial_equity_cad",
        "no_trading_benchmark_cad",
        "net_external_flows_cad",
        "valuation_inputs_json",
    ),
    "balances.csv": (
        "session_id",
        "exchange",
        "asset",
        "account_role",
        "quantity",
        "as_of",
        "ledger_entries_json",
    ),
    "fees.csv": (
        "id",
        "trade_id",
        "category",
        "quantity",
        "asset",
        "cad_value",
        "effective_at",
        "fee_version_id",
        "fx_quote_id",
        "assumptions_json",
    ),
    "system_health.csv": (
        "id",
        "created_at",
        "exchange",
        "severity",
        "event_type",
        "message",
        "details_json",
    ),
}
EXPORT_FILES = ("SUMMARY.md", "metrics.json", *CSV_COLUMNS, "config_snapshot.json")


class ReportPeriod(Contract):
    start: UTCTimestamp
    end: UTCTimestamp

    @model_validator(mode="after")
    def ordered(self) -> "ReportPeriod":
        if self.end <= self.start:
            raise ValueError("Report end must be after start")
        return self


class SimulationCounts(Contract):
    attempts: int = Field(ge=0)
    pending: int = Field(ge=0)
    completed: int = Field(ge=0)
    partial: int = Field(ge=0)
    missed: int = Field(ge=0)
    failed: int = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> "SimulationCounts":
        if (
            self.attempts
            != self.pending + self.completed + self.partial + self.missed + self.failed
        ):
            raise ValueError("Simulation counts must reconcile")
        return self


class ExchangePairResult(Contract):
    buy_exchange: str
    sell_exchange: str
    net_simulated_realized_pnl_cad: ExactDecimal
    completed_count: int = Field(ge=0)


class DailySummary(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["paper"] = "paper"
    period: ReportPeriod
    generated_at: UTCTimestamp
    session_ids: tuple[UUID, ...]
    calculation_version: str
    opportunities_detected: int = Field(ge=0)
    telegram_alerts_delivered: int = Field(ge=0)
    simulations: SimulationCounts
    gross_simulated_profit_cad: ExactDecimal | None
    trading_fees_cad: NonNegativeDecimal | None
    estimated_expenses_cad: NonNegativeDecimal | None
    net_simulated_realized_pnl_cad: ExactDecimal | None
    unrealized_pnl_cad: ExactDecimal | None
    portfolio_equity_cad: ExactDecimal | None
    initial_portfolio_cad: NonNegativeDecimal | None
    no_trading_benchmark_cad: ExactDecimal | None
    net_external_flows_cad: ExactDecimal | None
    capital_utilization_pct: NonNegativeDecimal | None = Field(le=100)
    maximum_drawdown_pct: NonNegativeDecimal | None = Field(le=100)
    best_exchange_pair: ExchangePairResult | None
    worst_exchange_pair: ExchangePairResult | None
    failure_reasons: dict[str, Annotated[int, Field(ge=0)]]
    data_quality_notes: tuple[str, ...]
    assumptions: dict[str, str]


class ExportManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["paper"] = "paper"
    generated_at: UTCTimestamp
    period: ReportPeriod
    session_ids: tuple[UUID, ...]
    files: tuple[str, ...] = EXPORT_FILES
    timestamp_format: Literal["ISO-8601 UTC"] = "ISO-8601 UTC"
    decimal_format: Literal["base-10 strings"] = "base-10 strings"
    reporting_currency: Literal["CAD"] = "CAD"

    @model_validator(mode="after")
    def stable_files(self) -> "ExportManifest":
        if self.files != EXPORT_FILES:
            raise ValueError("Export v1.0 requires the complete ordered file manifest")
        return self


class ReportService(Protocol):
    async def daily_summary(self, period: ReportPeriod, session_id: UUID) -> DailySummary: ...
    async def export_zip(self, period: ReportPeriod, session_id: UUID) -> bytes: ...


class AnalyticsExportMetrics(Contract):
    """Stage5's actual numerical export envelope; legacy daily schemas remain intact."""

    schema_version: Literal["quantix-export-v1"] = "quantix-export-v1"
    paper_only: Literal[True] = True
    session_id: UUID
    name: str
    classification: Literal["VALIDATED", "RESEARCH"]
    period: ReportPeriod
    strategy_version_id: UUID | None
    metrics: dict
    portfolio: dict | None
    opening_portfolio: dict | None
    initial_equity_cad: NonNegativeDecimal
    no_trading_initial_conditions: dict
    opportunity_detection_count: int = Field(ge=0)
    opportunity_count_scope: str
    notification_counts: dict = Field(default_factory=dict)
    return_on_mean_committed_capital_pct: ExactDecimal | None
    by_trade_size: list[dict]
    expense_breakdown: dict
    strategy_versions: list[dict]
    strategy_version_window_limited: bool
    reconciliation: dict
    limitations: list[str]
    accounting_convention: str
    reporting_timezone: Literal["America/Edmonton"]
    currency: Literal["CAD"]
    statistical_warning: str
    generated_at: UTCTimestamp
    clock: dict
    dataset_scope: str


class ArchiveFile(Contract):
    name: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)
    records: int | None
    columns: list[str] | None = None


class AnalyticsExportManifest(Contract):
    schema_version: Literal["quantix-export-v1"] = "quantix-export-v1"
    paper_only: Literal[True] = True
    generated_at: UTCTimestamp
    period: ReportPeriod
    session_id: UUID
    classification: Literal["VALIDATED", "RESEARCH"]
    strategy_version_id: UUID | None
    required_files: tuple[str, ...] = EXPORT_FILES
    files: list[ArchiveFile]
    hash_algorithm: str
    limits: dict[str, int]
    reconciliation: dict
    complete: Literal[True] = True

    @model_validator(mode="after")
    def complete_files(self):
        if self.required_files != EXPORT_FILES:
            raise ValueError("Mandatory export headers/files cannot change silently")
        if not set(EXPORT_FILES).issubset({f.name for f in self.files}):
            raise ValueError("Analytical archive is missing mandatory files")
        if len({f.name for f in self.files}) != len(self.files):
            raise ValueError("Duplicate archive member")
        return self
