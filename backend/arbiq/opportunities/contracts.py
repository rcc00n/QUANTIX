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


@dataclass(frozen=True, slots=True)
class MarketInput:
    market_id: str
    venue: str
    base_currency: str
    quote_currency: str
    asks: tuple[BookLevel, ...]
    bids: tuple[BookLevel, ...]
    fee: Fee
    rules: MarketRules
    fx: FXConversion | None = None

    def __post_init__(self) -> None:
        if (
            not self.market_id
            or not self.base_currency
            or not self.quote_currency
            or not self.venue
        ):
            raise ValueError("Market identity is required")
        if self.base_currency == self.quote_currency:
            raise ValueError("Market base and quote must differ")
        if not self.asks or not self.bids:
            raise ValueError("Both book sides are required")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("Crossed/locked books are not executable evidence")
        if any(a.price >= b.price for a, b in zip(self.asks, self.asks[1:], strict=False)):
            raise ValueError("Asks must be strictly increasing")
        if any(a.price <= b.price for a, b in zip(self.bids, self.bids[1:], strict=False)):
            raise ValueError("Bids must be strictly decreasing")
        if self.fx and self.fx.currency != self.quote_currency:
            raise ValueError("FX currency differs from market quote")
        if self.rules.price_tick:
            with localcontext() as context:
                context.prec = 78
                if any(level.price % self.rules.price_tick for level in (*self.asks, *self.bids)):
                    raise ValueError("Observed price conflicts with exchange price precision")


@dataclass(frozen=True, slots=True)
class Costs:
    slippage_bps: Decimal = Decimal(10)
    rebalancing_bps: Decimal = Decimal(10)
    fixed_rebalancing_cad: Decimal = Decimal(0)
    verified: bool = False
    source: str = "conservative configurable reserves; not observed fills"

    def __post_init__(self) -> None:
        for value in (self.slippage_bps, self.rebalancing_bps, self.fixed_rebalancing_cad):
            _number(value)


@dataclass(frozen=True, slots=True)
class Evaluation:
    buy_quantity: Decimal
    base_acquired: Decimal
    sell_quantity: Decimal
    sell_base_required: Decimal
    residual_base: Decimal
    buy_vwap: Decimal
    sell_vwap: Decimal
    buy_notional: Decimal
    sell_notional: Decimal
    buy_fee_amount: Decimal
    sell_fee_amount: Decimal
    buy_quote_required: Decimal
    sell_quote_received: Decimal
    acquisition_cost_cad: Decimal | None
    sale_proceeds_cad: Decimal | None
    gross_profit_cad: Decimal | None
    fees_cad: Decimal | None
    fx_cost_cad: Decimal | None
    expenses_cad: Decimal | None
    net_profit_cad: Decimal | None
    net_return_pct: Decimal | None
    net_profit_quote: Decimal | None
    verified: bool
    reasons: tuple[str, ...]
    capital_requirements: dict[str, dict[str, Decimal]]
    inventory_projection: dict[str, dict[str, Decimal]]
    slippage_reserve_cad: Decimal | None = None
    rebalancing_cost_cad: Decimal | None = None
    buy_fee_cad: Decimal | None = None
    sell_fee_cad: Decimal | None = None
    base_fee_value_cad: Decimal | None = None
    fee_treatment: str = (
        "Base fees affect quantities and are valued at executed-side VWAP for disclosure; "
        "quote fees affect cashflows. Net profit subtracts each cost once."
    )
    gross_profit_definition: str = (
        "Projected net cashflow before reserves plus disclosed trading fees."
    )
    fx_cost_definition: str = (
        "Half of executable quote-currency round-trip drag; diagnostic already included "
        "in acquisition and liquidation cashflows, never an additional deduction."
    )
    mode: Literal["PRE_FUNDED"] = "PRE_FUNDED"
    version: str = CALCULATION_VERSION


@dataclass(frozen=True, slots=True)
class BudgetResult:
    budget_cad: Decimal
    best: Evaluation | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    best: Evaluation | None
    maximum: Evaluation | None
    budgets: tuple[BudgetResult, ...]
    evaluated_count: int
    reasons: tuple[str, ...]
    optimality: str = "bounded depth-breakpoint search with exact quantity rounding"


@dataclass(frozen=True, slots=True)
class TransferAssumptions:
    network: str | None
    network_verified: bool
    withdrawal_enabled: bool
    deposit_enabled: bool
    withdrawal_fee_base: Decimal
    network_fee_base: Decimal
    confirmations: int | None
    estimated_seconds: int | None
    future_price_haircut_pct: Decimal
    evidence: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value in (
            self.withdrawal_fee_base,
            self.network_fee_base,
            self.future_price_haircut_pct,
        ):
            _number(value)
        if self.future_price_haircut_pct >= 100:
            raise ValueError("Future price haircut must be below 100 percent")
        if self.confirmations is not None and self.confirmations < 0:
            raise ValueError("Invalid confirmations")
        if self.estimated_seconds is not None and self.estimated_seconds <= 0:
            raise ValueError("Transfer delay must be positive")


