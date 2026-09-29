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


@dataclass(frozen=True, slots=True)
class AllocationConstraints:
    capital_cad: Decimal
    target_trade_cad: Decimal = Decimal(250)
    min_buy_capital_cad: Decimal = Decimal(50)
    min_cell_cad: Decimal = Decimal(25)
    max_venue_pct: Decimal = Decimal(60)
    max_asset_pct: Decimal = HUNDRED
    max_reserve_pct: Decimal = HUNDRED
    max_stablecoin_pct: Decimal = Decimal(50)
    min_venues: int = 2
    reserve_pct: Decimal = Decimal(10)

    def __post_init__(self):
        if _exact(self.capital_cad) <= 0 or _exact(self.target_trade_cad) <= 0:
            raise ValueError("Capital and target must be positive")
        if _exact(self.min_buy_capital_cad) < 0 or _exact(self.min_cell_cad) < 0:
            raise ValueError("Minimum allocation and buy capital must be nonnegative")
        if type(self.min_venues) is not int or not 1 <= self.min_venues <= MAX_VENUES:
            raise ValueError("A bounded minimum paid-in venue count is required")
        shares = (
            self.max_venue_pct,
            self.max_asset_pct,
            self.max_reserve_pct,
            self.max_stablecoin_pct,
            self.reserve_pct,
        )
        if any(not ZERO <= _exact(share) <= HUNDRED for share in shares):
            raise ValueError("Funding constraint percentages must be within 0 to 100")


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    policy: AllocationPolicy
    constraints: AllocationConstraints
    allocations: tuple[PricedAllocation, ...]
    unallocated_cad: Decimal
    feasible: bool
    reasons: tuple[str, ...]
    allocation_summary: dict
    coverage_by_size: tuple[dict, ...]
    route_capacity: tuple[dict, ...]
    median_funded_acquisition_cad: Decimal | None

    @decimal_context
    def as_payload(self) -> dict:
        return {
            "allocation_version": VERSION,
            "policy": self.policy,
            "capital_cad": _fmt(self.constraints.capital_cad),
            "trade_amount_cad": _fmt(self.constraints.target_trade_cad),
            "constraints": {
                definition.name: _fmt(value) if isinstance(value, Decimal) else value
                for definition in fields(self.constraints)
                for value in (getattr(self.constraints, definition.name),)
            },
            "allocations": [allocation.payload() for allocation in self.allocations],
            "total_cad": _fmt(self.constraints.capital_cad - self.unallocated_cad),
            "unallocated_cad": _fmt(self.unallocated_cad),
            "feasible": self.feasible,
            "reasons": list(self.reasons),
            "allocation_summary": self.allocation_summary,
            **_inventory_payload(
                self.constraints.target_trade_cad,
                self.coverage_by_size,
                self.route_capacity,
                self.median_funded_acquisition_cad,
            ),
        }


