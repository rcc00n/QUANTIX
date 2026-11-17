"""Synthetic retained evidence for informational classifications and ranking."""

from datetime import timedelta
from decimal import Decimal

from arbiq.opportunities import api
from test_opportunity_api import (
    NOW,
    synthetic_health,
    synthetic_payload,
    synthetic_route,
)


def capture(profit="1.000000000000000001", **edits):
    return synthetic_payload(
        profit=profit,
        raw_price_discrepancy=True,
        costs={
            "gross_profit_cad": "2",
            "buy_quantity": "0.25",
            "sell_quantity": "0.25",
            "buy_quote_required": "160",
            "sell_base_required": "0.25",
            "capital_requirements": {"synthetic-a": {"CAD": "160"}},
        },
        maximum_executable={"acquisition_cost_cad": "500.001234567890123456"},
        evidence={
            "buy_quality": "HEALTHY",
            "sell_quality": "HEALTHY",
            "fee_assumptions": {
                "buy": {
                    "account_tier_verified": False,
                    "fee_evidence_status": "PUBLIC_SCHEDULE_VERIFIED",
                },
                "sell": {
                    "account_tier_verified": False,
                    "fee_evidence_status": "CONSERVATIVE_ESTIMATE",
                },
            },
            "timing": {
                "clock_trusted": True,
                "cross_book_uncertainty_seconds": 0.5,
                "cross_book_receive_skew_seconds": 1.5,
                "cross_book_source_skew_seconds": None,
            },
        },
        **edits,
    )


def test_distinct_screen_gross_depth_funding_fee_eligibility_and_actionability():
    payload = capture()
    result = api.project(
        synthetic_route(payload),
        Decimal(1000),
        synthetic_health(),
        30,
        maximum_book_skew=2,
    )
    states = result["classifications"]
    assert states == {
        "evidence_basis": "retained_evaluation",
        "freshness": "RETAINED",
        "raw_price_discrepancy": True,
        "theoretically_positive": True,
        "depth_feasible": True,
        "depth_executable_positive": True,
        "inventory_funded": None,
        "fee_verified": False,
        "fee_evidence": {
            "buy": "PUBLIC_SCHEDULE_VERIFIED",
            "sell": "CONSERVATIVE_ESTIMATE",
        },
        "account_eligible": None,
        "actionable": False,
        "receipts_within_skew_limit": True,
        "sources_within_skew_limit": None,
    }
    assert result["maximum_liquidity_cad"] == "500.001234567890123456"
    assert result["required_inventory"] == {"synthetic-a": {"CAD": "160"}}
    assert "classifications" not in payload


def test_nonpositive_liquidity_remains_feasible_and_unknown_legacy_inputs_stay_unknown():
    loss = api.project(
        synthetic_route(capture("-0.000000000000000001")),
        Decimal(1000),
        synthetic_health(),
        30,
    )
    assert loss["classifications"]["theoretically_positive"] is True
    assert loss["classifications"]["depth_feasible"] is True
    assert loss["classifications"]["depth_executable_positive"] is False
    missing = api.project(synthetic_route(), Decimal(1000), synthetic_health(), 30)
    assert missing["classifications"]["raw_price_discrepancy"] is None
    assert missing["classifications"]["depth_executable_positive"] is None
    assert missing["classifications"]["fee_verified"] is None
    blocked = synthetic_payload(
        profit=None, quantity=None, reasons=["NO_FEASIBLE_EXECUTABLE_SIZE"]
    )
    state = api.project(
        synthetic_route(blocked), Decimal(1000), synthetic_health(), 30
    )["classifications"]
    assert state["depth_feasible"] is False
    assert state["depth_executable_positive"] is False


def test_live_freshness_requires_trusted_current_receipts_and_skew_uses_uncertainty():
    payload = capture()
    timing = payload["evidence"]["timing"]
    timing["cross_book_source_skew_seconds"] = 1.6
    route = synthetic_route(payload)
    params = {"current": payload, "maximum_book_skew": 2}
    live = api.project(route, Decimal(1000), synthetic_health(), 30, **params)
    assert live["classifications"]["freshness"] == "CURRENT"
    assert live["classifications"]["receipts_within_skew_limit"] is True
    assert live["classifications"]["sources_within_skew_limit"] is False
    stale = api.project(
        route,
        Decimal(1000),
        synthetic_health(NOW + timedelta(seconds=31)),
        30,
        **params,
    )
    assert stale["classifications"]["freshness"] == "STALE"
    assert stale["classifications"]["actionable"] is False
    uncertain = api.project(
        route, Decimal(1000), synthetic_health(trusted=False), 30, **params
    )
    assert uncertain["classifications"]["freshness"] == "UNVERIFIED"


def test_exact_net_profit_dominates_return_percentage_then_evidence_breaks_ties():
    ordinary = {
        "id": "ordinary",
        "net_profit_cad": "5.000000000000000001",
        "net_return_pct": "0.2",
        "data_confidence": "HEALTHY",
        "classifications": {"freshness": "CURRENT", "receipts_within_skew_limit": True},
    }
    smaller = {
        **ordinary,
        "id": "smaller",
        "net_profit_cad": "5.000000000000000000",
        "net_return_pct": "5",
    }
    stale = {**ordinary, "id": "aaa-stale", "classifications": {"freshness": "STALE"}}
    narrow = {
        **ordinary,
        "id": "zulu-depth",
        "maximum_liquidity_cad": "250.000000000000000001",
    }
    deeper = {
        **narrow,
        "id": "beta-depth",
        "maximum_liquidity_cad": "250.000000000000000002",
    }
    unknown = {**ordinary, "id": "unknown", "net_profit_cad": "NaN"}
    items = [ordinary, smaller, stale, unknown, narrow, deeper]
    ordered = sorted(items, key=lambda row: api.financial_sort(row, "net_profit_cad"))
    assert [row["id"] for row in ordered] == [
        "beta-depth",
        "zulu-depth",
        "ordinary",
        "aaa-stale",
        "smaller",
        "unknown",
    ]
    assert (
        min(items, key=lambda row: api.financial_sort(row, "net_return_pct")) == smaller
    )
