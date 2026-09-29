"""Bounded allocation of independent, pre-priced two-leg paper inventory.

Route sizing and executable-depth valuation happen before this pure calculation.
The planner never interpolates marks or assumes parity between quote assets.
"""

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from arbiq.finance.contracts import exact_decimal, utc_timestamp
from arbiq.portfolio.contracts import SCALE, decimal_context, stored

AllocationPolicy = Literal["LIQUIDITY_WEIGHTED", "ROUTE_OPTIMIZED", "DIVERSIFIED_HYBRID"]
VERSION = "quantix-allocation-v2"
TRADE_SIZES = tuple(Decimal(value) for value in (50, 100, 250, 500, 1000, 2500, 5000))
CAD = "fiat:CAD"
ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
MAX_ROUTES = 40
MAX_ROUTE_OPTIONS = 360
MAX_VENUES = 30
MAX_ACTIVE_CELLS = 60


def _exact(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError("Allocation inputs must be Decimal")
    return exact_decimal(value)


def _money(value: Decimal) -> Decimal:
    return value.quantize(SCALE, rounding=ROUND_DOWN)


def _fmt(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True, order=True)
class Cell:
    exchange: str
    asset: str

    def __post_init__(self):
        if not self.exchange or not self.asset:
            raise ValueError("Funding cell requires an exchange and canonical asset")


@dataclass(frozen=True, slots=True)
class PricedAllocation:
    cell: Cell
    quantity: Decimal
    cad_rate: Decimal
    evidence: Mapping = field(default_factory=dict, compare=False)
    cad_value: Decimal = field(init=False)

    @decimal_context
    def __post_init__(self):
        if _exact(self.quantity) <= 0 or _exact(self.cad_rate) <= 0:
            raise ValueError("Funding quantities and sourced CAD rates must be positive")
        if self.cell.asset == CAD and self.cad_rate != ONE:
            raise ValueError("CAD allocation must use the CAD identity")
        value = stored(self.quantity * self.cad_rate)
        if value is None or value <= 0:
            raise ValueError("Funding contribution must have a positive CAD basis")
        object.__setattr__(self, "cad_value", value)

    def payload(self) -> dict:
        return {
            "exchange": self.cell.exchange,
            "asset": self.cell.asset,
            "quantity": _fmt(self.quantity),
            "cad_rate": _fmt(self.cad_rate),
            "cad_value": _fmt(self.cad_value),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RouteDemand:
    route_id: str
    budget_cad: Decimal
    acquisition_cad: Decimal
    buy: PricedAllocation
    sell: PricedAllocation
    net_profit_cad: Decimal | None
    frequency: int = 1
    liquidity_cad: Decimal | None = None
    reliability: Decimal = ONE
    evaluation_id: str | None = None
    evaluated_at: datetime | None = None
    projected_costs: Mapping[str, str | None] | None = field(default=None, compare=False)

    def __post_init__(self):
        if not self.route_id or self.buy.cell.exchange == self.sell.cell.exchange:
            raise ValueError("A funding route requires two different venues")
        if not ZERO < _exact(self.acquisition_cad) <= _exact(self.budget_cad):
            raise ValueError("Actual acquisition must be positive and within its CAD budget")
        if self.net_profit_cad is not None:
            _exact(self.net_profit_cad)
        if self.liquidity_cad is not None and _exact(self.liquidity_cad) <= 0:
            raise ValueError("Supplied liquidity must be positive")
        if not ZERO <= _exact(self.reliability) <= ONE:
            raise ValueError("Route confidence must be between zero and one")
        if type(self.frequency) is not int or not 1 <= self.frequency <= 1_000_000:
            raise ValueError("Positive bounded historical count required")
        if self.evaluated_at is not None:
            utc_timestamp(self.evaluated_at)

    @property
    @decimal_context
    def pair_cad(self) -> Decimal:
        return self.buy.cad_value + self.sell.cad_value

    @property
    def profitable(self) -> bool:
        return self.net_profit_cad is not None and self.net_profit_cad > ZERO


