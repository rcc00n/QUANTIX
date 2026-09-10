"""Immutable deterministic inputs and projections. These are never orders or fills."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Literal

from arbiq.finance.contracts import exact_decimal, utc_timestamp
from arbiq.market_data.contracts import BookLevel

CALCULATION_VERSION = "quantix-depth-v1.0.0"
DEFAULT_BUDGETS = tuple(Decimal(x) for x in (100, 250, 500, 1000, 2000, 5000, 10000))


def _number(value: Decimal, *, positive: bool = False) -> None:
    exact_decimal(value)
    if value < 0 or (positive and value == 0):
        raise ValueError("Expected a positive/nonnegative Decimal")


@dataclass(frozen=True, slots=True)
class Fee:
    rate: Decimal
    currency: Literal["base", "quote"] = "quote"
    verified: bool = False
    source: str = "unverified conservative assumption"
    version: str | None = None
    execution: Literal["taker"] = "taker"

    def __post_init__(self) -> None:
        _number(self.rate)
        if self.rate >= 1 or self.currency not in ("base", "quote"):
            raise ValueError("Invalid taker fee")
        if self.execution != "taker":
            raise ValueError("Immediate execution requires taker fees")


