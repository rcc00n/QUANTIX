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


def _timestamp(raw: datetime | str) -> datetime:
    return utc_timestamp(raw if isinstance(raw, datetime) else datetime.fromisoformat(raw))


def _causal_mark(allocation: PricedAllocation, as_of: datetime) -> bool:
    if allocation.cell.asset == CAD:
        return True
    evidence = allocation.evidence.get("mark", allocation.evidence)
    if not isinstance(evidence, Mapping):
        return False
    try:
        received = _timestamp(evidence["received_at"])
        observed = evidence.get("observed_at")
        return received <= as_of and (observed is None or _timestamp(observed) <= received)
    except (KeyError, TypeError, ValueError):
        return False


def _exposures(allocations, group, stablecoins):
    by_venue, by_asset = defaultdict(Decimal), defaultdict(Decimal)
    stable = ZERO
    for item in allocations:
        by_venue[group(item.cell.exchange)] += item.cad_value
        by_asset[item.cell.asset] += item.cad_value
        if item.cell.asset in stablecoins:
            stable += item.cad_value
    return by_venue, by_asset, stable


def _reserves(active, constraints, reserve_venues, group, stablecoins):
    total = constraints.capital_cad
    allocated = sum((a.cad_value for a in active), ZERO)
    left = min(
        max(ZERO, total - allocated),
        _money(total * constraints.max_reserve_pct / HUNDRED),
    )
    by_venue, by_asset, _ = _exposures(active, group, stablecoins)
    asset_room = max(ZERO, _money(total * constraints.max_asset_pct / HUNDRED) - by_asset[CAD])
    left = min(left, asset_room)
    venue_cap = _money(total * constraints.max_venue_pct / HUNDRED)
    reserves = []
    for venue in sorted(reserve_venues, key=lambda v: (by_venue[group(v)], v)):
        venue_room = max(ZERO, venue_cap - by_venue[group(venue)])
        quantity = min(left, venue_room)
        if quantity:
            reserves.append(
                PricedAllocation(
                    Cell(venue, CAD), quantity, ONE, {"source": "CAD_IDENTITY", "reserve": True}
                )
            )
            by_venue[group(venue)] += quantity
            left -= quantity
        if left == 0:
            break
    reserve_cad = sum((a.cad_value for a in reserves), ZERO)
    return reserves, reserve_cad, total - allocated - reserve_cad


def _rank(route, policy, active, group):
    liquidity = route.liquidity_cad or route.acquisition_cad
    reliability = route.reliability
    profit = route.net_profit_cad or ZERO
    # Count retained eligible observations as frequency evidence. This score ranks
    # alternatives only; it is never used as an estimated annual return.
    opportunity = profit * route.frequency * reliability
    if policy == "LIQUIDITY_WEIGHTED":
        return (-(liquidity * reliability), -route.frequency, -opportunity, route.route_id)
    if policy == "ROUTE_OPTIMIZED":
        return (-opportunity, -(profit / route.pair_cad), -liquidity, route.route_id)
    funded_groups = {group(cell.exchange) for cell in active}
    new_groups = len(
        {group(route.buy.cell.exchange), group(route.sell.cell.exchange)} - funded_groups
    )
    new_assets = len(
        {route.buy.cell.asset, route.sell.cell.asset} - {cell.asset for cell in active}
    )
    return (
        not route.profitable,
        -new_groups,
        -new_assets,
        -(reliability * liquidity),
        -opportunity,
        route.route_id,
    )


def _fits(route: RouteDemand, available: Mapping[Cell, Decimal]) -> bool:
    return all(available.get(item.cell, ZERO) >= item.quantity for item in (route.buy, route.sell))


def _bounded_route_options(
    demands: Sequence[RouteDemand], group: Callable[[str], str], as_of: datetime | None
) -> tuple[dict[str, list[RouteDemand]], set[str]]:
    if len(demands) > MAX_ROUTE_OPTIONS or len({d.route_id for d in demands}) > MAX_ROUTES:
        raise ValueError("Funding evidence exceeds the bounded planning window")
    if as_of is not None:
        utc_timestamp(as_of)
    evidence_by_route: dict[str, list[RouteDemand]] = defaultdict(list)
    seen = set()
    excluded = set()
    for route in sorted(demands, key=lambda d: (d.route_id, d.budget_cad)):
        key = route.route_id, route.budget_cad
        if key in seen:
            raise ValueError("Each route can supply one exact observation per budget")
        seen.add(key)
        if group(route.buy.cell.exchange) == group(route.sell.cell.exchange):
            excluded.add("SAME_LIQUIDITY_VENUE")
            continue
        if as_of is not None and (
            route.evaluated_at is None
            or route.evaluated_at > as_of
            or not _causal_mark(route.buy, as_of)
            or not _causal_mark(route.sell, as_of)
        ):
            excluded.add("CAUSAL_FUNDING_EVIDENCE_UNAVAILABLE")
            continue
        evidence_by_route[route.route_id].append(route)
    return evidence_by_route, excluded


@decimal_context
def optimize_allocation(
    policy: AllocationPolicy,
    constraints: AllocationConstraints,
    demands: Sequence[RouteDemand],
    reserve_venues: Sequence[str],
    stablecoin_assets: Sequence[str] = (),
    *,
    venue_groups: Mapping[str, str] | None = None,
    as_of: datetime | None = None,
) -> AllocationPlan:
    """Select complete observed pairs, prefer target size, and retain capped CAD reserves.

    Different routes may reuse an existing quantity in the alternative count.
    Simultaneous count consumes held units each time and is a feasible lower bound.
    Reserve-labelled cash is not used to claim planning coverage. A resulting venue
    CAD account can use the cash under its separately selected live paper strategy.
    """
    if policy not in ("LIQUIDITY_WEIGHTED", "ROUTE_OPTIMIZED", "DIVERSIFIED_HYBRID"):
        raise ValueError("Unsupported paired allocation policy")
    if len(reserve_venues) > MAX_VENUES:
        raise ValueError("Funding evidence exceeds the bounded planning window")
    stablecoins = set(stablecoin_assets)
    reserves = set(reserve_venues)
    venue_groups = venue_groups or {}

    def group(exchange):
        return venue_groups.get(exchange, exchange)

    active: dict[Cell, PricedAllocation] = {}
    evidence_by_route, excluded = _bounded_route_options(demands, group, as_of)

    eligible = {
        rid: [
            r
            for r in options
            if r.acquisition_cad >= constraints.min_buy_capital_cad
            and (r.profitable or policy != "ROUTE_OPTIMIZED")
        ]
        for rid, options in evidence_by_route.items()
    }
    chosen = {}
    for target_sized in (True, False):
        remaining = {}
        for rid, options in eligible.items():
            if rid in chosen:
                continue
            sized = [
                r
                for r in options
                if (r.acquisition_cad >= constraints.target_trade_cad) == target_sized
            ]
            if target_sized:
                sized.sort(key=lambda r: (r.acquisition_cad, r.budget_cad))
            else:
                sized.sort(key=lambda r: (-r.acquisition_cad, r.budget_cad))
            if sized:
                remaining[rid] = sized
        while remaining:
            rid = min(remaining, key=lambda key: _rank(remaining[key][0], policy, active, group))
            for route in remaining.pop(rid):
                trial, _ = _try_pair(active, route, constraints, group, stablecoins)
                if trial is not None:
                    active, chosen[rid] = trial, route
                    break

    # After broad target placement, upgrade where larger observed sizes fit. Larger
    # independently priced cells replace smaller ones, so liquidity is never counted twice.
    for route in sorted(
        (
            route
            for options in eligible.values()
            for route in options
            if route.acquisition_cad > constraints.target_trade_cad
        ),
        key=lambda r: (_rank(r, policy, active, group), -r.acquisition_cad, r.budget_cad),
    ):
        selected = chosen.get(route.route_id)
        if selected is None or route.acquisition_cad <= selected.acquisition_cad:
            continue
        trial, _ = _try_pair(active, route, constraints, group, stablecoins)
        if trial is not None:
            active, chosen[route.route_id] = trial, route

    reserves, reserve_cad, unallocated = _reserves(
        tuple(active.values()), constraints, reserves, group, stablecoins
    )
    allocations = tuple(active[key] for key in sorted(active)) + tuple(reserves)
    deployed = sum((item.cad_value for item in active.values()), ZERO)
    total = constraints.capital_cad
    active_venues = len({group(key.exchange) for key in active})
    by_exchange, by_asset, stable = _exposures(allocations, lambda exchange: exchange, stablecoins)
    by_group, _, _ = _exposures(allocations, group, stablecoins)

    def share(value):
        return _fmt(_money(value * HUNDRED / total))

    def breakdown(values):
        return {
            k: {"cad_value": _fmt(v), "share_pct": share(v)} for k, v in sorted(values.items()) if v
        }

    summary = {
        "allocated_cad": _fmt(total - unallocated),
        "invested_cad": _fmt(deployed),
        "reserve_cad": _fmt(reserve_cad),
        "reserve_pct": share(reserve_cad),
        "by_exchange": breakdown(by_exchange),
        "by_venue_group": breakdown(by_group),
        "by_asset": breakdown(by_asset),
        "stablecoin_cad": _fmt(stable),
        "stablecoin_pct": share(stable),
        "funded_venues": active_venues,
        "funded_cells": len(active),
    }
    available = {cell: item.quantity for cell, item in active.items()}
    coverage, details, median = _inventory_metrics(
        evidence_by_route, available, constraints, group, stablecoins
    )
    failures = []
    if unallocated:
        failures.append("UNPLACED_CAD_WITHIN_RESERVE_AND_CONCENTRATION_LIMITS")
    if reserve_cad * HUNDRED < total * constraints.reserve_pct:
        failures.append("REQUIRED_RESERVE_UNFUNDED")
    if active_venues < constraints.min_venues:
        failures.append("MINIMUM_FUNDED_VENUES_UNAVAILABLE")
    reasons = failures + sorted(excluded)
    if not any(
        row["funded_alternative_routes"]
        for row in coverage
        if Decimal(row["trade_amount_cad"]) == constraints.target_trade_cad
    ):
        reasons.append("TARGET_TRADE_SIZE_NOT_FUNDED")
    all_routes = [r for options in evidence_by_route.values() for r in options]
    if not any(
        route.profitable and route.acquisition_cad >= constraints.min_buy_capital_cad
        for route in all_routes
    ):
        reasons.append(
            "NO_POSITIVE_SIZED_ROUTE"
            if any(r.net_profit_cad is not None for r in all_routes)
            else "PROFITABILITY_EVIDENCE_UNAVAILABLE"
        )
    return AllocationPlan(
        policy,
        constraints,
        allocations,
        unallocated,
        not failures,
        tuple(reasons),
        summary,
        coverage,
        details,
        median,
    )
