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
























