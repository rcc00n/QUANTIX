"""Versioned paper strategies. All profiles are counterfactual, never real latency claims."""

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select

from arbiq.db.models import StrategyVersion, SystemEvent, User
from arbiq.finance.contracts import Contract, NonNegativeDecimal, PositiveDecimal
from arbiq.paper_trading.models import StrategyHead


class TransferPolicy(Contract):
    network: str = Field(default="", max_length=80)
    network_verified: bool = False
    withdrawal_enabled: bool = False
    deposit_enabled: bool = False
    withdrawal_fee_base: NonNegativeDecimal = "0"
    network_fee_base: NonNegativeDecimal = "0"
    costs_verified: bool = False
    confirmations: int = Field(default=0, ge=0, le=10000)
    settlement_seconds: int = Field(default=600, ge=1, le=604800)
    haircut_pct: NonNegativeDecimal = Field(default="5", lt=100)


class PaperStrategy(Contract):
    schema_version: Literal["2.0"] = "2.0"
    preset: Literal["Conservative", "Balanced", "Aggressive Research", "Custom"] = "Balanced"
    starting_capital_cad: PositiveDecimal = "10000"
    trade_amount_cad: PositiveDecimal = "1000"
    min_buy_capital_cad: NonNegativeDecimal = "50"
    minimum_profit_cad: NonNegativeDecimal = "5"
    minimum_profit_pct: NonNegativeDecimal = "0.5"
    threshold_logic: Literal["AND", "OR"] = "AND"
    minimum_liquidity_cad: NonNegativeDecimal = "100"
    allowed_exchanges: tuple[str, ...] = ()
    allowed_assets: tuple[str, ...] = ()
    reaction_delay_seconds: int = Field(default=30, ge=0, le=3600)
    execution_profile: Literal["MANUAL", "FAST_COUNTERFACTUAL", "CUSTOM"] = "MANUAL"
    maximum_concurrent: int = Field(default=3, ge=1, le=20)
    maximum_simulations_per_hour: int = Field(default=60, ge=1, le=1000)
    maximum_capital_per_exchange_pct: PositiveDecimal = Field(default="75", le=100)
    maximum_inventory_exposure_pct: PositiveDecimal = Field(default="75", le=100)
    maximum_loss_cad: NonNegativeDecimal = "50"
    taker_fee_rate: NonNegativeDecimal = Field(default="0.006", lt=1)
    fee_verified: bool = False
    buy_fee_currency: Literal["base", "quote"] = "quote"
    sell_fee_currency: Literal["base", "quote"] = "quote"
    slippage_bps: NonNegativeDecimal = Field(default="10", le=10000)
    maximum_slippage_bps: NonNegativeDecimal | None = Field(default=None, le=10000)
    rebalance_bps: NonNegativeDecimal = Field(default="10", le=10000)
    mode: Literal["PRE_FUNDED", "TRANSFER", "RESEARCH_SHADOW"] = "PRE_FUNDED"
    research_execution_model: Literal["PRE_FUNDED", "TRANSFER"] = "PRE_FUNDED"
    minimum_confidence: Literal["HEALTHY", "DEGRADED", "ANOMALY_RESEARCH"] = "HEALTHY"
    maximum_book_age_seconds: int = Field(default=30, ge=1, le=60)
    allow_missing_source_timestamp: bool = False
    transfer: TransferPolicy = Field(default_factory=TransferPolicy)

    @model_validator(mode="after")
    def coherent(self):
        if self.maximum_slippage_bps is not None and self.slippage_bps > self.maximum_slippage_bps:
            raise ValueError("Modeled slippage exceeds the owner's maximum acceptable slippage")
        if self.mode != "RESEARCH_SHADOW" and (
            self.minimum_confidence != "HEALTHY" or self.allow_missing_source_timestamp
        ):
            raise ValueError("Unverified data confidence requires a separate research portfolio")
        if len(self.allowed_exchanges) > 30 or len(self.allowed_assets) > 100:
            raise ValueError("Bounded allowlists required")
        return self


