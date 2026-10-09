"""SYNTHETIC public book fixtures and isolated PostgreSQL planner checks."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_paper_execution as execution
import test_stage5_funding as stage5
from arbiq.db.models import ArbitrageEvaluation, ArbitrageRoute, Exchange, OrderBookSnapshot
from arbiq.opportunities.analysis import dump_input
from arbiq.paper_trading.funding import proposal_marks_fresh
from arbiq.paper_trading.funding_sizing import projected_costs, read_pair, size_at_budget
from arbiq.portfolio.accounting import reconcile
from arbiq.settings.strategy import presets
from sqlalchemy import select

D = Decimal
AT = datetime(2026, 12, 12, 12, tzinfo=UTC)
funding_api = stage5.funding_api
private_api = stage5.private_api
environment = stage5.environment


def test_receipt_only_valuation_is_considered_without_initializing_portfolio():
    payload = {
        "allocations": [
            {
                "asset": "native:SYNTHETIC",
                "evidence": {"mark": {"observed_at": None, "received_at": AT.isoformat()}},
            }
        ]
    }
    assert proposal_marks_fresh(payload, AT + timedelta(seconds=1)) is False


def pinned(buy=None, sell=None, when=AT):
    buy = buy or execution.book(uuid4(), "synthetic-buy", when)
    sell = sell or execution.book(uuid4(), "synthetic-sell", when, bid="110", ask="111")
    refs = {
        k: uuid4()
        for k in (
            "buy_snapshot_id",
            "sell_snapshot_id",
            "buy_fee_version_id",
            "sell_fee_version_id",
        )
    }
    return SimpleNamespace(
        id=uuid4(),
        created_at=when,
        evaluated_at=when,
        **refs,
        evidence={
            "clock": {"trusted": True},
            "buy_book": buy.model_dump(mode="json"),
            "sell_book": sell.model_dump(mode="json"),
            "calculation_inputs": {
                "evaluated_at": when.isoformat(),
                "buy": dump_input(execution.inputs(buy, "synthetic-buy")),
                "sell": dump_input(execution.inputs(sell, "synthetic-sell")),
            },
        },
    )


def test_sized_pair_funds_exactly_both_native_reservations_and_respects_inventory():
    strategy = presets()["Aggressive Research"].model_copy(update={"sell_fee_currency": "base"})
    pair = read_pair(pinned(), AT, strategy)
    point = size_at_budget(pair, D("500"), at_least=True)
    assert point is not None and point.acquisition_cad >= 500
    assert point.buy_quote > point.evaluation.buy_quote_required
    assert point.sell_base > point.evaluation.sell_quantity
    capped = size_at_budget(pair, D("500"), buy_available=D("7"), sell_available=point.sell_base)
    assert capped is None or capped.acquisition_cad < 50
    assert (
        size_at_budget(pair, D("500"), buy_available=point.buy_quote, sell_available=D("0")) is None
    )
    under_cap = size_at_budget(
        pair, D("500"), buy_available=point.buy_quote, sell_available=point.sell_base
    )
    assert under_cap and under_cap.reserved_acquisition_cad <= 500


def test_projected_costs_preserve_exact_values_zero_and_missing_evidence():
    strategy = presets()["Aggressive Research"]
    point = size_at_budget(read_pair(pinned(), AT, strategy), D("500"), at_least=True)
    assert point is not None
    evaluation = replace(
        point.evaluation,
        fees_cad=D("0.123456789012345678901"),
        fx_cost_cad=None,
        slippage_reserve_cad=D("0.000000000000000001"),
        rebalancing_cost_cad=D("0"),
    )
    assert projected_costs(evaluation) == {
        "fees_cad": "0.123456789012345678901",
        "fx_drag_cad": None,
        "slippage_reserve_cad": "0.000000000000000001",
        "rebalancing_cad": "0",
    }


@pytest.mark.parametrize(
    "failure", ["future_record", "future_book", "stale_source", "anomaly", "missing_fx"]
)
def test_funding_replay_rejects_noncausal_or_unpriced_evidence(failure):
    row = pinned()
    row.evidence = deepcopy(row.evidence)
    if failure == "future_record":
        row.created_at += timedelta(microseconds=1)
    elif failure == "future_book":
        row.evidence["buy_book"]["received_at"] = (AT + timedelta(seconds=1)).isoformat()
    elif failure == "stale_source":
        row.evidence["sell_book"]["exchange_timestamp"] = (AT - timedelta(seconds=90)).isoformat()
    elif failure == "anomaly":
        row.evidence["sell_book"]["state"] = "SUSPICIOUS"
    else:
        row.evidence["calculation_inputs"]["buy"]["fx"] = None
    with pytest.raises(ValueError):
        read_pair(row, AT, presets()["Aggressive Research"])


async def test_compare_and_select_immutable_proposal_once(funding_api, environment, monkeypatch):
    c, sessions, _asset = funding_api
    clock = {"now": AT + timedelta(seconds=1)}

    async def now(_request):
        return clock["now"]

    monkeypatch.setattr("arbiq.paper_trading.funding.now_checked", now)
    env = environment.ids
    source = pinned(
        execution.book(env["bm"], "synthetic-buy", AT),
        execution.book(env["sm"], "synthetic-sell", AT, bid="110", ask="111"),
    )
    async with environment.sessions() as db:
        venues = [await db.get(Exchange, key) for key in (env["bx"], env["sx"])]
        for venue in venues:
            venue.enabled = True
        route = await db.scalar(
            select(ArbitrageRoute).where(ArbitrageRoute.buy_market_id == env["bm"])
        )
        old = await db.scalar(
            select(ArbitrageEvaluation).where(ArbitrageEvaluation.route_id == route.id)
        )
        snaps = []
        for side, key in (("buy", "bm"), ("sell", "sm")):
            b = source.evidence[f"{side}_book"]
            s = OrderBookSnapshot(
                market_id=env[key],
                observed_at=AT,
                received_at=AT,
                bids=[[x["price"], x["quantity"]] for x in b["normalized"]["bids"]],
                asks=[[x["price"], x["quantity"]] for x in b["normalized"]["asks"]],
                source="SYNTHETIC",
                quality=b["state"],
                content_sha256=b["content_sha256"],
                created_at=AT,
            )
            db.add(s)
            snaps.append(s)
        await db.flush()
        db.add(
            ArbitrageEvaluation(
                route_id=route.id,
                revision=old.revision + 1,
                evaluated_at=AT,
                created_at=AT,
                buy_snapshot_id=snaps[0].id,
                sell_snapshot_id=snaps[1].id,
                buy_fee_version_id=old.buy_fee_version_id,
                sell_fee_version_id=old.sell_fee_version_id,
                net_profit_cad=D("10"),
                calculation_version="SYNTHETIC",
                evidence=source.evidence,
                result={},
            )
        )
        await db.commit()
    body = {
        "capital_cad": "1000",
        "trade_amount_cad": "250",
        "max_venue_pct": "100",
        "max_asset_pct": "100",
        "max_reserve_pct": "100",
        "exchange_slugs": [v.slug for v in venues],
    }
    response = c.post("/api/v1/paper/funding/compare", json=body)
    assert response.status_code == 200, response.text
    plans = response.json()["proposals"]
    assert [p["policy"] for p in plans] == [
        "COVERAGE",
        "LIQUIDITY_WEIGHTED",
        "ROUTE_OPTIMIZED",
        "DIVERSIFIED_HYBRID",
    ]
    assert len({p["as_of"] for p in plans}) == 1
    assert all(D(p["total_cad"]) == 1000 for p in plans)
    selected = plans[-1]
    assert selected["feasible"] and selected["creatable"] and selected["funded_route_count"] == 1
    costs = selected["route_capacity"][0]["target_pinned"]["projected_costs"]
    assert set(costs) == {"fees_cad", "fx_drag_cad", "slippage_reserve_cad", "rebalancing_cad"}
    assert isinstance(costs["fees_cad"], str) and D(costs["fees_cad"]) > 0
    assert not plans[0]["creatable"]
    blocked = c.post("/api/v1/paper/funding/compare", json={**body, "eligibility": "VALIDATED"})
    assert blocked.status_code == 422
    created_body = {
        "name": "SYNTHETIC selected research",
        "proposal_id": selected["id"],
        "capital_cad": "1000",
        "idempotency_key": "SYNTHETIC-select-one",
    }
    path = "/api/v1/paper/funding/experiments"
    submissions = await asyncio.gather(
        asyncio.to_thread(c.post, path, json=created_body),
        asyncio.to_thread(
            c.post, path, json={**created_body, "idempotency_key": "SYNTHETIC-concurrent-key"}
        ),
    )
    assert all(item.status_code == 201 for item in submissions)
    obs, concurrent = (item.json() for item in submissions)
    assert concurrent["id"] == obs["id"]
    assert {obs["reused"], concurrent["reused"]} == {True, False}
    assert obs["classification"] == "RESEARCH"
    changed = c.post(path, json={**created_body, "capital_cad": "999"})
    assert changed.status_code == 409
    clock["now"] += timedelta(seconds=40)
    stale = c.post(
        "/api/v1/paper/funding/experiments", json={**created_body, "proposal_id": plans[1]["id"]}
    )
    assert stale.status_code == 409
    retry = c.post(
        "/api/v1/paper/funding/experiments",
        json={**created_body, "idempotency_key": "SYNTHETIC-another-key"},
    )
    assert retry.status_code == 201 and retry.json()["reused"] and retry.json()["id"] == obs["id"]
    async with sessions() as db:
        assert await reconcile(db, UUID(obs["id"])) is True
