from pydantic import BaseModel, ConfigDict, Field


class PaperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    bootstrap_portfolios: bool = True
    interval_seconds: float = Field(default=2, ge=0.5, le=30)
    valuation_interval_seconds: int = Field(default=60, ge=15, le=3600)
    maximum_pending: int = Field(default=100, ge=1, le=1000)
    maximum_observation_wait_seconds: int = Field(default=90, ge=5, le=600)
    maximum_candidates_per_tick: int = Field(default=10, ge=1, le=100)
