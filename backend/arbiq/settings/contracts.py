from pydantic import Field

from arbiq.finance.contracts import Contract, NonNegativeDecimal, PositiveDecimal


class StrategySettings(Contract):
    schema_version: str = "1.0"
    minimum_profit_pct: NonNegativeDecimal = "0.5"
    minimum_profit_cad: NonNegativeDecimal = "5"
    trading_capital_cad: PositiveDecimal = "1000"
    reaction_delay_ms: int = Field(default=30000, ge=0, le=300000)
    max_book_age_ms: int = Field(default=2000, ge=100, le=60000)
    max_snapshot_skew_ms: int = Field(default=500, ge=0, le=10000)
    slippage_reserve_bps: NonNegativeDecimal = "10"
    rebalance_reserve_cad: NonNegativeDecimal = "0"
    exchange_allowlist: tuple[str, ...] = ()
    telegram_enabled: bool = False
