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


