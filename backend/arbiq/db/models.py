"""Persistent domain records. Financial amounts use exact NUMERIC(38,18).

JSONB stores decimal strings, never JSON floating point amounts. Domain writes and
cross-record invariants belong in services, not ORM callbacks. See docs/SCHEMA.md.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arbiq.db.base import Base, Record


class User(Record, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class LoginAttempt(Record, Base):
    __tablename__ = "login_attempts"
    username: Mapped[str] = mapped_column(String(80))
    client_hash: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool]
    __table_args__ = (Index("ix_login_throttle", "client_hash", "created_at"),)


class Exchange(Record, Base):
    __tablename__ = "exchanges"
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(default=False)
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict)
    config_version: Mapped[int] = mapped_column(default=1)


class Asset(Record, Base):
    __tablename__ = "assets"
    canonical_key: Mapped[str] = mapped_column(String(120), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # crypto / fiat / stablecoin
    network: Mapped[str | None] = mapped_column(String(100))
    identifiers: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (CheckConstraint("kind IN ('crypto','fiat','stablecoin')", name="kind"),)


class Market(Record, Base):
    __tablename__ = "markets"
    exchange_id: Mapped[UUID] = mapped_column(ForeignKey("exchanges.id"), index=True)
    base_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    quote_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    native_symbol: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(default=True)
    amount_step: Mapped[Decimal | None]
    price_tick: Mapped[Decimal | None]
    min_notional: Mapped[Decimal | None]
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        UniqueConstraint("exchange_id", "native_symbol"),
        CheckConstraint("base_asset_id <> quote_asset_id", name="distinct_assets"),
        CheckConstraint("amount_step > 0 AND price_tick > 0 AND min_notional >= 0", name="limits"),
    )


class StrategyVersion(Record, Base):
    __tablename__ = "strategy_versions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int]
    settings: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("user_id", "version"),)


class FeeVersion(Record, Base):
    __tablename__ = "fee_versions"
    exchange_id: Mapped[UUID] = mapped_column(ForeignKey("exchanges.id"))
    market_id: Mapped[UUID | None] = mapped_column(ForeignKey("markets.id"))
    effective_at: Mapped[datetime]
    maker_rate: Mapped[Decimal]
    taker_rate: Mapped[Decimal]
    source: Mapped[str] = mapped_column(Text)
    assumptions: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("maker_rate BETWEEN -1 AND 1 AND taker_rate BETWEEN 0 AND 1", name="rates"),
    )


class FXQuote(Record, Base):
    __tablename__ = "fx_quotes"
    base_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    quote_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    rate: Mapped[Decimal]
    observed_at: Mapped[datetime] = mapped_column(index=True)
    source: Mapped[str] = mapped_column(Text)
    provenance: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (CheckConstraint("rate > 0", name="positive_rate"),)


class MarketObservation(Record, Base):
    __tablename__ = "market_observations"
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"))
    observed_at: Mapped[datetime]
    received_at: Mapped[datetime]
    bid: Mapped[Decimal | None]
    ask: Mapped[Decimal | None]
    bid_quantity: Mapped[Decimal | None]
    ask_quantity: Mapped[Decimal | None]
    source: Mapped[str] = mapped_column(String(40))
    quality: Mapped[str] = mapped_column(String(32))
    quality_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_observations_market_time", "market_id", "observed_at"),)


class OrderBookSnapshot(Record, Base):
    __tablename__ = "order_book_snapshots"
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"))
    observed_at: Mapped[datetime]
    received_at: Mapped[datetime]
    bids: Mapped[list] = mapped_column(JSONB)  # [[price_decimal_string, quantity_decimal_string]]
    asks: Mapped[list] = mapped_column(JSONB)
    sequence: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(40))
    quality: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (Index("ix_books_market_time", "market_id", "observed_at"),)


class Opportunity(Record, Base):
    __tablename__ = "opportunities"
    buy_market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"))
    sell_market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"))
    buy_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("order_book_snapshots.id"))
    sell_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("order_book_snapshots.id"))
    buy_fee_version_id: Mapped[UUID] = mapped_column(ForeignKey("fee_versions.id"))
    sell_fee_version_id: Mapped[UUID] = mapped_column(ForeignKey("fee_versions.id"))
    strategy_version_id: Mapped[UUID] = mapped_column(ForeignKey("strategy_versions.id"))
    fx_quote_id: Mapped[UUID] = mapped_column(ForeignKey("fx_quotes.id"))
    detected_at: Mapped[datetime] = mapped_column(index=True)
    expires_at: Mapped[datetime]
    quantity: Mapped[Decimal]
    estimated_gross_cad: Mapped[Decimal]
    estimated_fees_cad: Mapped[Decimal]
    estimated_expenses_cad: Mapped[Decimal]
    estimated_net_cad: Mapped[Decimal]
    estimated_net_pct: Mapped[Decimal]
    calculation_version: Mapped[str] = mapped_column(String(40))
    assumptions: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("buy_market_id <> sell_market_id", name="distinct_markets"),
        CheckConstraint("quantity > 0 AND expires_at >= detected_at", name="valid_opportunity"),
    )


class Signal(Record, Base):
    __tablename__ = "signals"
    opportunity_id: Mapped[UUID] = mapped_column(ForeignKey("opportunities.id"), index=True)
    strategy_version_id: Mapped[UUID] = mapped_column(ForeignKey("strategy_versions.id"))
    decision: Mapped[str] = mapped_column(String(20))  # alert / suppressed / rejected
    reason: Mapped[str] = mapped_column(Text)
    evaluated_at: Mapped[datetime]
    rules_snapshot: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("decision IN ('alert','suppressed','rejected')", name="decision"),
    )


class NotificationAttempt(Record, Base):
    __tablename__ = "notification_attempts"
    signal_id: Mapped[UUID] = mapped_column(ForeignKey("signals.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))
    delivered_at: Mapped[datetime | None]
    provider_message_id: Mapped[str | None] = mapped_column(String(120))
    error_code: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        UniqueConstraint("signal_id", "channel", "attempt"),
        CheckConstraint(
            "status IN ('pending','delivered','failed') AND attempt > 0", name="status"
        ),
    )


class PaperSession(Record, Base):
    __tablename__ = "paper_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    strategy_version_id: Mapped[UUID] = mapped_column(ForeignKey("strategy_versions.id"))
    name: Mapped[str] = mapped_column(String(120))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    initial_equity_cad: Mapped[Decimal]
    benchmark_snapshot: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (CheckConstraint("initial_equity_cad >= 0", name="initial_equity"),)


class VirtualAccount(Record, Base):
    __tablename__ = "virtual_accounts"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"))
    exchange_id: Mapped[UUID | None] = mapped_column(ForeignKey("exchanges.id"))
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    role: Mapped[str] = mapped_column(String(24))  # available / reserved / equity / expense
    __table_args__ = (
        UniqueConstraint(
            "session_id", "exchange_id", "asset_id", "role", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint("role IN ('available','reserved','equity','expense')", name="role"),
        CheckConstraint("role IN ('equity','expense') OR exchange_id IS NOT NULL", name="exchange"),
    )


class Journal(Record, Base):
    __tablename__ = "journals"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    effective_at: Mapped[datetime]
    source_id: Mapped[UUID | None]
    provenance: Mapped[dict] = mapped_column(JSONB)


class LedgerEntry(Record, Base):
    __tablename__ = "ledger_entries"
    journal_id: Mapped[UUID] = mapped_column(ForeignKey("journals.id"), index=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("virtual_accounts.id"), index=True)
    quantity: Mapped[Decimal]
    cad_value: Mapped[Decimal | None]
    memo: Mapped[str] = mapped_column(Text)
    __table_args__ = (CheckConstraint("quantity <> 0", name="nonzero_quantity"),)


class VirtualDeposit(Record, Base):
    __tablename__ = "virtual_deposits"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("virtual_accounts.id"))
    journal_id: Mapped[UUID] = mapped_column(ForeignKey("journals.id"), unique=True)
    fx_quote_id: Mapped[UUID] = mapped_column(ForeignKey("fx_quotes.id"))
    quantity: Mapped[Decimal]
    cad_equivalent: Mapped[Decimal]
    is_initial: Mapped[bool]
    __table_args__ = (CheckConstraint("quantity > 0 AND cad_equivalent >= 0", name="deposit"),)


class BalanceSnapshot(Record, Base):
    __tablename__ = "balance_snapshots"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("virtual_accounts.id"))
    as_of: Mapped[datetime]
    quantity: Mapped[Decimal]
    last_journal_id: Mapped[UUID] = mapped_column(ForeignKey("journals.id"))
    __table_args__ = (Index("ix_balances_account_time", "account_id", "as_of"),)


class PaperTrade(Record, Base):
    __tablename__ = "paper_trades"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    opportunity_id: Mapped[UUID | None] = mapped_column(ForeignKey("opportunities.id"))
    route_id: Mapped[UUID | None] = mapped_column(ForeignKey("arbitrage_routes.id"))
    evaluation_id: Mapped[UUID | None] = mapped_column(ForeignKey("arbitrage_evaluations.id"))
    strategy_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("strategy_versions.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(180), unique=True)
    classification: Mapped[str] = mapped_column(String(16), default="VALIDATED")
    mode: Mapped[str] = mapped_column(String(24), default="PRE_FUNDED")
    requested_quantity: Mapped[Decimal | None]
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24))
    requested_at: Mapped[datetime]
    reaction_delay_ms: Mapped[int] = mapped_column(Integer)
    eligible_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    reason: Mapped[str | None] = mapped_column(Text)
    calculation_version: Mapped[str] = mapped_column(String(40))
    assumptions: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        Index("ix_paper_status_eligible", "status", "eligible_at"),
        Index("ix_paper_session_requested", "session_id", "requested_at"),
        CheckConstraint(
            "status IN ('pending','completed','partial','missed','failed','CANDIDATE',"
            "'ELIGIBILITY_CHECK','SCHEDULED','RECHECKING','EXECUTING','COMPLETED',"
            "'PARTIAL','MISSED','FAILED','EXPIRED')",
            name="status",
        ),
        CheckConstraint("reaction_delay_ms >= 0 AND eligible_at >= requested_at", name="delay"),
    )


class PaperOrder(Record, Base):
    __tablename__ = "paper_orders"
    trade_id: Mapped[UUID] = mapped_column(ForeignKey("paper_trades.id"), index=True)
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id"))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal]
    limit_price: Mapped[Decimal | None]
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell') AND quantity > 0", name="side_quantity"),
        CheckConstraint(
            "status IN ('pending','filled','partial','failed','cancelled')", name="status"
        ),
    )


class PaperFill(Record, Base):
    __tablename__ = "paper_fills"
    order_id: Mapped[UUID] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("order_book_snapshots.id"))
    journal_id: Mapped[UUID] = mapped_column(ForeignKey("journals.id"), unique=True)
    fee_version_id: Mapped[UUID] = mapped_column(ForeignKey("fee_versions.id"))
    fx_quote_id: Mapped[UUID | None] = mapped_column(ForeignKey("fx_quotes.id"))
    quantity: Mapped[Decimal]
    price: Mapped[Decimal]
    fee_quantity: Mapped[Decimal]
    fee_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    quote_notional: Mapped[Decimal | None]
    fee_cad: Mapped[Decimal | None]
    cost_basis_cad: Mapped[Decimal | None]
    realized_simulated_pnl_cad: Mapped[Decimal | None]
    executed_at: Mapped[datetime]
    provenance: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint(
            "quantity > 0 AND price > 0 AND fee_quantity >= 0 AND fee_cad >= 0", name="fill_values"
        ),
    )


class SimulationEvent(Record, Base):
    __tablename__ = "simulation_events"
    trade_id: Mapped[UUID] = mapped_column(ForeignKey("paper_trades.id"), index=True)
    order_id: Mapped[UUID | None] = mapped_column(ForeignKey("paper_orders.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime]
    provenance: Mapped[dict] = mapped_column(JSONB)


