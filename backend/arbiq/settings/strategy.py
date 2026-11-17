"""Versioned paper strategies. All profiles are counterfactual, never real latency claims."""

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select

from arbiq.db.models import StrategyVersion, SystemEvent, User
from arbiq.finance.contracts import Contract, NonNegativeDecimal, PositiveDecimal
from arbiq.paper_trading.models import StrategyHead

StrategyLabProfile = Literal["LIQUID_ARBITRAGE", "ANOMALY_HUNTER", "HYBRID_OPTIMIZER", "NO_TRADING"]


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
    strategy_lab_profile: StrategyLabProfile | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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
        if self.strategy_lab_profile and (
            self.mode != "RESEARCH_SHADOW"
            or self.research_execution_model != "PRE_FUNDED"
            or self.minimum_confidence != "HEALTHY"
            or self.allow_missing_source_timestamp
            or self.threshold_logic != "AND"
            or self.minimum_profit_cad <= 0
            or self.minimum_profit_pct <= 0
        ):
            raise ValueError(
                "Strategy Lab requires healthy, prefunded research with positive profit thresholds"
            )
        return self


def strategy_lab_settings(profile: StrategyLabProfile) -> PaperStrategy:
    risk_settings = {
        "LIQUID_ARBITRAGE": {
            "minimum_profit_cad": "10",
            "minimum_profit_pct": "1",
            "maximum_concurrent": 1,
            "maximum_loss_cad": "20",
        },
        "ANOMALY_HUNTER": {
            "maximum_concurrent": 1,
            "maximum_simulations_per_hour": 20,
            "maximum_loss_cad": "25",
        },
        "HYBRID_OPTIMIZER": {},
        "NO_TRADING": {},
    }
    return PaperStrategy(
        preset="Custom",
        strategy_lab_profile=profile,
        mode="RESEARCH_SHADOW",
        maximum_capital_per_exchange_pct="60",
        maximum_inventory_exposure_pct="70",
        **risk_settings[profile],
    )


def read_pinned_strategy(settings: dict) -> PaperStrategy:
    """Read immutable legacy settings without changing their historical behavior.

    New strategy requests default to C$50. Versions written before this independent
    constraint existed had no minimum-buy rule; an absent field means zero only
    when reading an already persisted version or scheduled trade.
    """
    return PaperStrategy.model_validate({"min_buy_capital_cad": "0", **settings})


def minimum_buy_satisfied(strategy: PaperStrategy, acquisition_cost_cad) -> bool:
    """Actual fee-inclusive acquisition capital, independently of profit OR/AND."""
    return acquisition_cost_cad is not None and acquisition_cost_cad >= strategy.min_buy_capital_cad


def presets():
    return {
        "Conservative": PaperStrategy(
            preset="Conservative",
            trade_amount_cad="500",
            minimum_profit_cad="10",
            minimum_profit_pct="1",
            minimum_liquidity_cad="500",
            maximum_concurrent=1,
            maximum_loss_cad="20",
        ),
        "Balanced": PaperStrategy(),
        "Aggressive Research": PaperStrategy(
            preset="Aggressive Research",
            mode="RESEARCH_SHADOW",
            trade_amount_cad="250",
            minimum_profit_cad="0",
            minimum_profit_pct="0",
            minimum_liquidity_cad="1",
            threshold_logic="OR",
            minimum_confidence="ANOMALY_RESEARCH",
            allow_missing_source_timestamp=True,
            maximum_concurrent=3,
            maximum_loss_cad="25",
        ),
    }


async def save_strategy(db, user_id, settings, now, reason="Owner updated paper strategy"):
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    version = (
        await db.scalar(
            select(func.max(StrategyVersion.version)).where(StrategyVersion.user_id == user_id)
        )
        or 0
    ) + 1
    row = StrategyVersion(
        user_id=user_id, version=version, settings=settings.model_dump(mode="json"), reason=reason
    )
    db.add(row)
    await db.flush()
    head = await db.get(StrategyHead, user_id)
    if head:
        head.strategy_version_id = row.id
        head.updated_at = now
    else:
        db.add(StrategyHead(user_id=user_id, strategy_version_id=row.id, updated_at=now))
    db.add(
        SystemEvent(
            actor_user_id=user_id,
            event_type="strategy.version_created",
            severity="info",
            message="Paper strategy version created",
            details={"version": version, "strategy_version_id": str(row.id)},
        )
    )
    return row
