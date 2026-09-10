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


@dataclass(frozen=True, slots=True)
class MarketRules:
    amount_step: Decimal | None = None
    min_quantity: Decimal | None = None
    min_notional: Decimal | None = None
    max_quantity: Decimal | None = None
    max_notional: Decimal | None = None
    verified: bool = False
    source: str = "exchange metadata"
    price_tick: Decimal | None = None

    def __post_init__(self) -> None:
        for name in (
            "amount_step",
            "min_quantity",
            "min_notional",
            "max_quantity",
            "max_notional",
            "price_tick",
        ):
            value = getattr(self, name)
            if value is not None:
                _number(value, positive=True)
        if self.min_quantity and self.max_quantity and self.min_quantity > self.max_quantity:
            raise ValueError("Inconsistent quantity limits")
        if self.min_notional and self.max_notional and self.min_notional > self.max_notional:
            raise ValueError("Inconsistent notional limits")


@dataclass(frozen=True, slots=True)
class ConversionLevel:
    """Derived internal rates may contain more than 18 decimal places.

    Original observed levels remain BookLevel/NUMERIC values. Reciprocals are never
    rounded to a fake exchange quote or stored in a NUMERIC rate column.
    """

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        for value in (self.price, self.quantity):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("Derived conversion levels require positive finite Decimals")


