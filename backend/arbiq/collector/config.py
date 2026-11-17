from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_EXCHANGES = [
    "kraken",
    "ndax",
    "coinbase",
    "coinbaseexchange",
    "cryptocom",
    "bitstamp",
    "gemini",
    "binance",
    "bybit",
    "okx",
    "bitfinex",
    "kucoin",
    "gate",
    "mexc",
    "bitget",
    "htx",
    "poloniex",
    "bitso",
    "luno",
    "bitbank",
]


class MarketProfile(StrEnum):
    LIQUID_MARKETS = "LIQUID_MARKETS"
    BROAD_EXPLORATION = "BROAD_EXPLORATION"
    BALANCED_ADAPTIVE = "BALANCED_ADAPTIVE"


class CollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    exchange_ids: list[str] = Field(default_factory=lambda: DEFAULT_EXCHANGES.copy())
    exclude_exchanges: list[str] = []
    base_assets: list[str] = []  # Empty means all discovered spot assets.
    quote_assets: list[str] = ["CAD", "USD", "USDC", "USDT", "EUR", "BTC", "JPY"]
    priority_assets: list[str] = ["BTC", "ETH", "SOL", "XRP", "LTC", "USDC", "USDT"]
    market_profile: MarketProfile | None = None
    active_only: bool = True
    allow_unknown_active: bool = True
    prefer_cad: bool = True
    minimum_quote_volume: Decimal = Decimal("0")  # Native quote units; no implicit FX.
    minimum_book_notional: Decimal = Decimal("0")
    max_markets_per_exchange: int = Field(default=10, ge=1, le=50)
    max_concurrency: int = Field(default=6, ge=1, le=32)
    depth: int = Field(default=25, ge=10, le=100)
    minimum_levels: int = Field(default=3, ge=1, le=100)
    fast_interval_seconds: float = Field(default=10, ge=2, le=300)
    slow_interval_seconds: float = Field(default=25, ge=5, le=600)
    minimum_request_interval_seconds: float = Field(default=1, ge=0.1, le=60)
    request_timeout_seconds: float = Field(default=15, ge=2, le=60)
    discovery_interval_seconds: int = Field(default=21600, ge=300, le=86400)
    memory_maintenance_interval_seconds: int = Field(default=300, ge=60, le=3600)
    maximum_age_seconds: int = Field(default=60, ge=1, le=600)
    freshness_fresh_seconds: float = Field(default=2, gt=0, le=60)
    freshness_stale_seconds: float = Field(default=30, ge=1, le=600)
    maximum_future_skew_seconds: int = Field(default=5, ge=1, le=60)
    anomaly_deviation_pct: Decimal = Decimal("10")
    persistence_interval_seconds: int = Field(default=30, ge=5, le=600)
    sample_interval_seconds: int = Field(default=300, ge=60, le=3600)
    sample_retention_hours: int = Field(default=24, ge=1, le=168)
    quality_retention_days: int = Field(default=7, ge=1, le=90)
    max_transient_rows: int = Field(default=100000, ge=1000, le=1000000)
    evidence_budget_per_hour: int = Field(default=24, ge=0, le=200)
    websocket_exchanges: list[str] = ["kraken", "coinbaseexchange", "okx"]
    websocket_fast_markets_per_exchange: int = Field(default=2, ge=0, le=4)
    websocket_publish_interval_seconds: float = Field(default=1, ge=0.25, le=30)
    websocket_idle_timeout_seconds: int = Field(default=45, ge=15, le=300)
    websocket_snapshot_timeout_seconds: int = Field(default=15, ge=2, le=60)
    retry_base_seconds: float = Field(default=2, ge=0.1, le=30)
    retry_cap_seconds: float = Field(default=300, ge=10, le=3600)

    @field_validator("minimum_quote_volume", "minimum_book_notional", "anomaly_deviation_pct")
    @classmethod
    def finite_nonnegative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("Thresholds must be finite and nonnegative")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.minimum_levels > self.depth:
            raise ValueError("minimum_levels cannot exceed requested depth")
        if self.fast_interval_seconds > self.slow_interval_seconds:
            raise ValueError("fast interval cannot exceed slow interval")
        if self.freshness_fresh_seconds > self.freshness_stale_seconds:
            raise ValueError("Fresh threshold cannot exceed stale threshold")
        if len(self.exchange_ids) != len(set(self.exchange_ids)):
            raise ValueError("Duplicate exchange identifiers")
        return self
