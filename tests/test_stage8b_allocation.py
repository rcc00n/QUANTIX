"""SYNTHETIC Stage 8B allocation inputs. No market reads, fills or ledger writes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from arbiq.paper_trading.allocation import (
    AllocationConstraints,
    Cell,
    PricedAllocation,
    RouteDemand,
    describe_inventory,
    optimize_allocation,
)
from arbiq.portfolio.contracts import stored

D = Decimal
NOW = datetime(2026, 11, 22, 15, tzinfo=UTC)
CAD = "fiat:CAD"
BTC = "synthetic:BTC"
ETH = "synthetic:ETH"
USDC = "synthetic:USDC"
POLICIES = ("LIQUIDITY_WEIGHTED", "ROUTE_OPTIMIZED", "DIVERSIFIED_HYBRID")


def marked(exchange, asset, quantity, cad_rate):
    return PricedAllocation(
        Cell(exchange, asset),
        D(quantity),
        D(cad_rate),
        {"source": "SYNTHETIC_DEPTH", "received_at": NOW.isoformat()},
    )


def route(route_id="r1", budget="500", amount="500", net="10", *, buy=None, sell=None):
    return RouteDemand(
        route_id,
        D(budget),
        D(amount),
        buy or marked("buy", CAD, amount, "1"),
        sell or marked("sell", BTC, amount, "1"),
        None if net is None else D(net),
        evaluation_id=f"synthetic-evaluation-{route_id}",
        evaluated_at=NOW,
    )


def band(plan, size):
    return next(row for row in plan.coverage_by_size if D(row["trade_amount_cad"]) == D(size))


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("capital", ["10000", "50000", "100000.000000000000000001"])
def test_fixed_capital_and_two_legs_in_original_currency(policy, capital):
    # The buying venue needs 600 synthetic USDC at a sourced CAD rate of 0.9,
    # while the seller needs a separate priced amount of BTC including its fee.
    demand = route(
        buy=marked("buy", USDC, "600", "0.9"),
        sell=marked("sell", BTC, "0.012345678901234567", "48000"),
    )
    rules = AllocationConstraints(D(capital), target_trade_cad=D(500))
    plan = optimize_allocation(policy, rules, [demand], ["buy", "sell"], [USDC], as_of=NOW)
    payload = plan.as_payload()
    with localcontext() as ctx:
        ctx.prec = 78
        assert sum((a.cad_value for a in plan.allocations), D(0)) == D(capital)
        for allocation in plan.allocations:
            assert allocation.cad_value == stored(allocation.quantity * allocation.cad_rate)
    assert payload["feasible"] is True and D(payload["unallocated_cad"]) == 0
    assert payload["funded_route_count"] == 1
    assert D(payload["allocation_summary"]["stablecoin_cad"]) == 540
    assert payload["allocation_summary"]["funded_cells"] == 2
    assert payload["route_capacity"][0]["actionable"] is False
    paired = payload["route_capacity"][0]["target_pinned"]
    assert paired["buy"]["required_quantity"] == "600"
    assert paired["sell"]["required_quantity"] == "0.012345678901234567"
    assert all(D(item["deficit_quantity"]) == 0 for item in paired["deficits"])


def test_nominal_budget_does_not_count_a_seven_dollar_result_or_round_up():
    rules = AllocationConstraints(D(10000), target_trade_cad=D(500))
    small = route("tiny", amount="7")
    rounded = route("rounded", amount="499.99", sell=marked("sell", BTC, "5", "110"))
    plan = optimize_allocation("DIVERSIFIED_HYBRID", rules, [small, rounded], ["buy", "sell"])
    assert band(plan, 500)["funded_alternative_routes"] == 0
    assert band(plan, 100)["funded_alternative_routes"] == 1
    assert band(plan, 50)["funded_alternative_routes"] == 1
    projected = {row["route_id"]: row for row in plan.route_capacity}
    assert projected["tiny"]["maximum_funded_acquisition_cad"] is None
    assert "BELOW_MINIMUM_BUY_CAPITAL" in projected["tiny"]["limiting_constraints"]
    assert projected["rounded"]["maximum_funded_acquisition_cad"] == "499.99"
    assert projected["rounded"]["target_pinned"]["target_met"] is False
    assert "TARGET_TRADE_SIZE_NOT_FUNDED" in plan.reasons


def test_larger_exact_priced_allocation_can_support_several_tiers():
    rules = AllocationConstraints(D(50000), target_trade_cad=D(500))
    plan = optimize_allocation(
        "ROUTE_OPTIMIZED",
        rules,
        [route(budget="5000", amount="5000", net="100"), route()],
        ["buy", "sell"],
    )
    assert band(plan, 5000)["funded_alternative_routes"] == 1
    assert band(plan, 2500)["funded_alternative_routes"] == 1
    assert plan.median_funded_acquisition_cad == 5000
    active = [item for item in plan.allocations if not item.evidence.get("reserve")]
    assert len(active) == 2 and all(item.quantity == 5000 for item in active)


def test_overlapping_alternatives_cannot_reserve_the_same_quote_twice():
    d1 = route()
    d2 = route("r2", sell=marked("other-sell", ETH, "1", "500"))
    plan = optimize_allocation(
        "LIQUIDITY_WEIGHTED",
        AllocationConstraints(D(10000)),
        [d1, d2],
        ["buy", "sell", "other-sell"],
    )
    tier = band(plan, 500)
    assert tier["funded_alternative_routes"] == 2
    assert tier["simultaneous_funded_routes"] == 1
    assert D(plan.allocation_summary["invested_cad"]) == 1500


def test_tight_reserve_venue_cap_keeps_unplaced_cash_and_exact_reconciliation():
    rules = AllocationConstraints(
        D(10000),
        max_venue_pct=D(35),
        max_asset_pct=D(60),
        max_reserve_pct=D(40),
    )
    plan = optimize_allocation("DIVERSIFIED_HYBRID", rules, [route()], ["buy", "sell"])
    payload = plan.as_payload()
    assert not plan.feasible
    assert "UNPLACED_CAD_WITHIN_RESERVE_AND_CONCENTRATION_LIMITS" in plan.reasons
    assert D(payload["allocation_summary"]["reserve_cad"]) == 4000
    assert sum((a.cad_value for a in plan.allocations), D(0)) + plan.unallocated_cad == 10000
    assert D(payload["total_cad"]) + D(payload["unallocated_cad"]) == 10000
    assert all(
        D(row["share_pct"]) <= 35 for row in payload["allocation_summary"]["by_exchange"].values()
    )
    assert all(
        D(row["share_pct"]) <= 60 for row in payload["allocation_summary"]["by_asset"].values()
    )


def test_stablecoin_cap_and_minimum_cell_reject_complete_pair_without_half_funding():
    rules = AllocationConstraints(D(10000), max_stablecoin_pct=D(1), min_cell_cad=D(250))
    usdc = route("stable", buy=marked("buy", USDC, "500", "1.02"))
    too_small = route("dust", sell=marked("sell", BTC, "2", "100"))
    plan = optimize_allocation(
        "LIQUIDITY_WEIGHTED", rules, [usdc, too_small], ["buy", "sell"], [USDC]
    )
    assert all(item.cell.asset == CAD for item in plan.allocations)
    assert plan.allocation_summary["funded_venues"] == 0
    assert not plan.feasible
    assert "MINIMUM_FUNDED_VENUES_UNAVAILABLE" in plan.reasons
    by_route = {row["route_id"]: row["limiting_constraints"] for row in plan.route_capacity}
    assert "STABLECOIN_EXPOSURE_LIMIT" in by_route["stable"]
    assert "BELOW_MINIMUM_USEFUL_CELL" in by_route["dust"]














