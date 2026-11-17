from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arbiq.opportunities.contracts import DEFAULT_BUDGETS


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    budgets_cad: list[Decimal] = Field(default_factory=lambda: list(DEFAULT_BUDGETS))
    default_budget_cad: Decimal = Decimal("1000")
    conservative_taker_rate: Decimal = Decimal("0.006")
    slippage_bps: Decimal = Decimal("10")
    rebalancing_bps: Decimal = Decimal("10")
    maximum_book_age_seconds: int = Field(default=30, ge=1, le=60)
    maximum_book_skew_seconds: int = Field(default=10, ge=1, le=30)
    route_ttl_seconds: int = Field(default=60, ge=5, le=300)
    persistence_interval_seconds: int = Field(default=120, ge=10, le=3600)
    max_writes_per_minute: int = Field(default=30, ge=1, le=300)
    max_candidates: int = Field(default=500, ge=10, le=5000)
    max_optimizer_evaluations: int = Field(default=128, ge=1, le=4096)
    evaluation_interval_seconds: float = Field(default=2, ge=0.5, le=30)

    @field_validator("conservative_taker_rate")
    @classmethod
    def fee_bound(cls, value):
        if not value.is_finite() or not Decimal("0.001") <= value <= Decimal("0.1"):
            raise ValueError("Conservative unverified taker estimate must be 0.1%–10%")
        return value

    @field_validator("budgets_cad")
    @classmethod
    def budgets_bound(cls, value):
        if (
            not value
            or len(value) > 20
            or any(not x.is_finite() or not 0 < x <= 1000000 for x in value)
        ):
            raise ValueError("One to twenty finite positive CAD budgets required")
        return sorted(set(value))

    @field_validator("default_budget_cad", "slippage_bps", "rebalancing_bps")
    @classmethod
    def nonnegative(cls, value):
        if not value.is_finite() or value < 0:
            raise ValueError("Finite nonnegative cost required")
        return value
