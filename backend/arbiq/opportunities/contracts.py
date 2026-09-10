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


@dataclass(frozen=True, slots=True)
class ConversionLeg:
    """Executable source→target levels; price is target units per source unit.

    For A/B bids: (bid, A depth). For B→A asks: (1/ask, ask*A depth).
    Fee is deducted from the target currency once. Levels must be best first.
    """

    source_currency: str
    target_currency: str
    levels: tuple[BookLevel | ConversionLevel, ...]
    fee_rate: Decimal = Decimal(0)
    evidence: str = ""

    def __post_init__(self) -> None:
        _number(self.fee_rate)
        if self.fee_rate >= 1 or self.source_currency == self.target_currency or not self.levels:
            raise ValueError("Invalid executable conversion leg")
        if any(a.price < b.price for a, b in zip(self.levels, self.levels[1:], strict=False)):
            raise ValueError("Conversion levels must have descending target/source rates")


@dataclass(frozen=True, slots=True)
class ConversionRoute:
    legs: tuple[ConversionLeg, ...] = ()

    def __post_init__(self) -> None:
        if len(self.legs) > 3:
            raise ValueError("Conversion routes are bounded to three hops")
        for a, b in zip(self.legs, self.legs[1:], strict=False):
            if a.target_currency != b.source_currency:
                raise ValueError("Disconnected conversion route")
        currencies = [self.legs[0].source_currency] if self.legs else []
        currencies.extend(leg.target_currency for leg in self.legs)
        if len(currencies) != len(set(currencies)):
            raise ValueError("Conversion cycles are forbidden")


@dataclass(frozen=True, slots=True)
class FXConversion:
    currency: str
    to_cad: ConversionRoute
    from_cad: ConversionRoute
    verified: bool = False
    fresh_until: datetime = datetime(2000, 1, 1, tzinfo=UTC)
    source: str = ""
    observed_at: datetime | None = None
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        utc_timestamp(self.fresh_until)
        for timestamp in (self.observed_at, self.available_at):
            if timestamp is not None:
                utc_timestamp(timestamp)
        if self.currency == "CAD":
            if self.to_cad.legs or self.from_cad.legs:
                raise ValueError("CAD identity must not fabricate conversion legs")
        elif not self.to_cad.legs or not self.from_cad.legs:
            raise ValueError("Both acquisition and liquidation conversion evidence are required")
        elif (
            self.to_cad.legs[0].source_currency != self.currency
            or self.to_cad.legs[-1].target_currency != "CAD"
            or self.from_cad.legs[0].source_currency != "CAD"
            or self.from_cad.legs[-1].target_currency != self.currency
        ):
            raise ValueError("FX routes have incorrect currency directions")


def identity_cad() -> FXConversion:
    return FXConversion(
        "CAD",
        ConversionRoute(),
        ConversionRoute(),
        True,
        datetime.max.replace(tzinfo=UTC),
        "CAD reporting currency identity",
    )


