"""Financial audit regressions. Every market, price and portfolio here is SYNTHETIC."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arbiq.opportunities import api as opportunity_api
from arbiq.opportunities.analysis import dump_input
from arbiq.opportunities.config import EngineConfig
from arbiq.opportunities.contracts import (
    ConversionRoute,
    Costs,
    Fee,
    FXConversion,
    MarketInput,
    MarketRules,
    identity_cad,
)
from arbiq.opportunities.diagnostics import size_diagnostics
from arbiq.opportunities.math import (
    conversion_leg_from_book,
    convert,
    evaluate_route,
    optimize_route,
    required_input,
)
from arbiq.paper_trading.execution import planned_reserves
from arbiq.settings.strategy import (
    PaperStrategy,
    minimum_buy_satisfied,
    read_pinned_strategy,
)
from fastapi import HTTPException
from pydantic import ValidationError
from test_arbitrage_math import levels

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)
NO_COST = Costs(D(0), D(0), D(0), True, "SYNTHETIC no-reserve scenario")


def synthetic_small_route():
    rules = MarketRules(D("0.00001"), D("0.00001"), D("0.01"), verified=True)
    buy = MarketInput(
        "SYNTHETIC-buy",
        "SYNTHETIC-venue-a",
        "SYNTHETIC-BTC",
        "CAD",
        levels((100, 20)),
        levels((99, 20)),
        Fee(D(0), verified=True),
        rules,
        identity_cad(),
    )
    sell = replace(
        buy,
        market_id="SYNTHETIC-sell",
        venue="SYNTHETIC-venue-b",
        asks=levels((120, 20)),
        bids=levels((110, ".0671"), (1, 20)),
    )
    return buy, sell


def pinned(buy, sell):
    return {
        "buy_quality": "HEALTHY",
        "sell_quality": "SUSPICIOUS",
        "calculation_inputs": {
            "buy": dump_input(buy),
            "sell": dump_input(sell),
            "costs": dump_input(NO_COST),
            "evaluated_at": NOW.isoformat(),
        },
    }


def test_tiny_size_is_correct_profit_optimum_not_entire_selected_budget():
    buy, sell = synthetic_small_route()
    value = optimize_route(
        buy, sell, budget_cad=D(1000), now=NOW, costs=NO_COST, budgets=()
    )
    assert value.best.acquisition_cost_cad == D("6.71")
    assert value.best.buy_quantity == D(".0671")
    assert value.best.net_profit_cad == D(".671")
    assert "certified" in value.optimality
    assert evaluate_route(buy, sell, D(".5"), now=NOW, costs=NO_COST).net_profit_cad < 0


def test_comparisons_price_actual_target_sizes_not_repeated_budget_optima():
    buy, sell = synthetic_small_route()
    result = size_diagnostics(pinned(buy, sell))
    assert D(result["best"]["acquisition_cost_cad"]) == D("6.71")
    assert [D(row["requested_buy_capital_cad"]) for row in result["comparisons"]] == [
        50,
        100,
        250,
        500,
        1000,
        2500,
        5000,
    ]
    for row in result["comparisons"][:5]:
        assert row["supported"]
        assert D(row["buy_capital_cad"]) == D(row["requested_buy_capital_cad"])
        assert D(row["estimated_net_profit_cad"]) < 0
    for row in result["comparisons"][5:]:
        assert not row["supported"]
        assert row["limiting_constraints"] == ["BUY_OBSERVED_DEPTH"]
    assert "BEST_SIZE_AT_SELL_TOP_LEVEL_DEPTH_BOUNDARY" in result["explanations"]
    assert "SUSPICIOUS_RETAINED_BOOK_NOT_VERIFIED" in result["explanations"]
    assert result["actionable"] is False and result["actual_execution"] is False


def test_unsupported_target_never_extrapolates_profit():
    buy, sell = synthetic_small_route()
    buy = replace(buy, asks=levels((100, 1)))
    result = size_diagnostics(pinned(buy, sell))
    assert result["comparisons"][1]["supported"]
    for row in result["comparisons"][2:]:
        assert not row["supported"]
        assert row["estimated_net_profit_cad"] is None
        assert row["projection"] is None


def test_certified_best_is_between_targets_and_discloses_exact_paper_reserves_and_costs():
    buy, sell = synthetic_small_route()
    buy = replace(buy, fee=Fee(D(".001")))
    sell = replace(
        sell, bids=levels((110, "7.50"), (1, 20)), fee=Fee(D(".002"), "base")
    )
    costs = Costs(D(10), D(20), D("0.25"))
    evidence = pinned(buy, sell)
    evidence["calculation_inputs"].update(
        costs=dump_input(costs), max_search_evaluations=128
    )
    result = size_diagnostics(evidence, D(1000), max_search_evaluations=256)
    best = result["best"]
    assert result["optimizer"]["max_search_evaluations"] == 128
    assert result["optimizer"]["certified"]
    assert D(500) < D(best["acquisition_cost_cad"]) < D(1000)
    assert (
        "NEXT_QUANTITY_STEP_DOES_NOT_IMPROVE_NET_PROFIT"
        in result["best_limiting_constraints"]
    )
    evaluated = evaluate_route(buy, sell, D(best["buy_quantity"]), now=NOW, costs=costs)
    expected_buy, expected_sell = planned_reserves(
        evaluated, buy.fee, sell.fee, costs.slippage_bps
    )
    displayed = result["best_costs_and_reserves"]
    assert D(displayed["planned_buy_reserve"]) == expected_buy
    assert D(displayed["planned_sell_reserve"]) == expected_sell
    assert displayed["estimated_costs"] == {
        "fees_cad": str(evaluated.fees_cad),
        "fx_drag_cad": str(evaluated.fx_cost_cad),
        "slippage_cad": str(evaluated.slippage_reserve_cad),
        "rebalancing_cad": str(evaluated.rebalancing_cost_cad),
    }


def test_size_diagnostics_retains_sided_failure_without_a_successful_curve():
    buy, sell = synthetic_small_route()
    sell = replace(sell, rules=replace(sell.rules, market_order_available=False))
    result = size_diagnostics(pinned(buy, sell))
    assert result["best"] is None
    assert result["best_limiting_constraints"] == ["SELL_MARKET_ORDER_UNAVAILABLE"]
    assert result["market_constraints"]["maximum_combined_buy_quantity"] is None
    assert all(
        item["limiting_constraints"] == ["SELL_MARKET_ORDER_UNAVAILABLE"]
        and not item["supported"]
        and item["estimated_costs"] is None
        for item in result["comparisons"]
    )


def test_market_cap_and_future_fx_keep_a_specific_binding_constraint():
    buy, sell = synthetic_small_route()
    limited = replace(sell, rules=replace(sell.rules, max_quantity=D(3)))
    result = size_diagnostics(pinned(buy, limited))
    assert result["comparisons"][3]["limiting_constraints"] == ["SELL_MAXIMUM_QUANTITY"]
    late = replace(
        sell, fx=replace(sell.fx, available_at=NOW + timedelta(microseconds=1))
    )
    result = size_diagnostics(pinned(buy, late))
    assert result["comparisons"][0]["limiting_constraints"] == [
        "SELL_CAD_CONVERSION_NOT_YET_AVAILABLE"
    ]
    assert result["comparisons"][0]["estimated_net_profit_cad"] is None


@pytest.mark.asyncio
async def test_sizing_endpoint_releases_database_before_offload_and_rejects_concurrent_work(
    monkeypatch,
):
    route_id, evaluation_id = uuid4(), uuid4()
    state = {"reads": 0, "committed": False}
    evidence = pinned(*synthetic_small_route())

    async def execute(_query):
        state["reads"] += 1
        return SimpleNamespace(one_or_none=lambda: (evaluation_id, evidence))

    async def commit():
        state["committed"] = True

    db = SimpleNamespace(execute=execute, commit=commit)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    arbitrage=EngineConfig(max_optimizer_evaluations=64)
                )
            )
        )
    )
    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(opportunity_api, "_projection_gate", gate)

    async def to_thread(function, *args, **kwargs):
        assert state["committed"]
        with pytest.raises(HTTPException) as blocked:
            await opportunity_api.diagnostics_sizes(route_id, request, db, None, D(500))
        assert blocked.value.status_code == 429
        assert blocked.value.headers == {"Retry-After": "2"}
        return function(*args, **kwargs)

    monkeypatch.setattr(
        opportunity_api, "asyncio", SimpleNamespace(to_thread=to_thread)
    )
    value = await opportunity_api.diagnostics_sizes(route_id, request, db, None, D(500))
    assert state["reads"] == 1
    assert value["evaluation_id"] == str(evaluation_id)
    assert value["optimizer"]["max_search_evaluations"] == 64


def test_minimum_buy_is_independent_of_maximum_profit_and_and_or():
    policy = PaperStrategy(
        min_buy_capital_cad="50",
        minimum_profit_cad="0",
        minimum_profit_pct="0",
        threshold_logic="OR",
        trade_amount_cad="1000",
        minimum_liquidity_cad="1",
    )
    assert not minimum_buy_satisfied(policy, D("6.71"))
    assert not minimum_buy_satisfied(policy, D("49.999999999999999999"))
    assert minimum_buy_satisfied(policy, D(50))
    assert not minimum_buy_satisfied(policy, None)
    assert policy.trade_amount_cad == D(1000) and policy.minimum_profit_cad == 0


def test_radar_budget_presets_preserve_legacy_choices_and_custom_preferences():
    assert EngineConfig().budgets_cad == list(
        map(D, (50, 100, 250, 500, 1000, 2000, 2500, 5000, 10000))
    )
    assert EngineConfig(budgets_cad=[D(10), D(2000)]).budgets_cad == [D(10), D(2000)]
    assert EngineConfig().default_budget_cad == D(1000)


def test_old_pinned_strategy_maintains_original_behavior_without_rewrite():
    raw = {"trade_amount_cad": "250", "preset": "Custom"}
    assert read_pinned_strategy(raw).min_buy_capital_cad == 0
    assert "min_buy_capital_cad" not in raw
    assert PaperStrategy.model_validate(raw).min_buy_capital_cad == 50
    assert (
        read_pinned_strategy({**raw, "min_buy_capital_cad": "25"}).min_buy_capital_cad
        == 25
    )


@pytest.mark.parametrize("logic", ["AND", "OR"])
def test_first_leg_recheck_does_not_allow_small_size_through_profit_logic(logic):
    from uuid import uuid4

    from arbiq.paper_trading.execution import recheck_projection
    from test_paper_execution import book, inputs, strategy

    buy = book(uuid4(), "SYNTHETIC-buy", NOW, qty="0.1")
    sell = book(uuid4(), "SYNTHETIC-sell", NOW, bid="110", ask="111", qty="0.1")
    with pytest.raises(ValueError, match="MINIMUM_BUY_CAPITAL"):
        recheck_projection(
            buy_book=buy,
            sell_book=sell,
            buy_input=inputs(buy, "a"),
            sell_input=inputs(sell, "b"),
            planned_quantity=D(".1"),
            decision_at=NOW,
            strategy=strategy(min_buy_capital_cad="50", threshold_logic=logic),
        )
    value = recheck_projection(
        buy_book=buy,
        sell_book=sell,
        buy_input=inputs(buy, "a"),
        sell_input=inputs(sell, "b"),
        planned_quantity=D(".1"),
        decision_at=NOW,
        strategy=strategy(min_buy_capital_cad="0", threshold_logic=logic),
    )
    assert value.acquisition_cost_cad < 50


@pytest.mark.parametrize("value", [-1, "-1", 1.25, "NaN", "Infinity"])
def test_minimum_buy_rejects_invalid_financial_values(value):
    with pytest.raises(ValidationError):
        PaperStrategy(min_buy_capital_cad=value)


def synthetic_fx(currency, bid, ask, fee=".002", depth="100000"):
    bids, asks = levels((bid, depth)), levels((ask, depth))
    return FXConversion(
        currency,
        ConversionRoute(
            (
                conversion_leg_from_book(
                    currency, "CAD", bids=bids, asks=asks, fee_rate=D(fee)
                ),
            )
        ),
        ConversionRoute(
            (
                conversion_leg_from_book(
                    currency, "CAD", bids=bids, asks=asks, reverse=True, fee_rate=D(fee)
                ),
            )
        ),
        True,
        NOW + timedelta(seconds=60),
        "SYNTHETIC FX with explicit spread",
        NOW - timedelta(seconds=1),
        NOW - timedelta(seconds=1),
    )


@pytest.mark.parametrize("buy_fee_currency", ["base", "quote"])
@pytest.mark.parametrize("sell_fee_currency", ["base", "quote"])
def test_cross_quote_cashflow_uses_acquisition_ask_liquidation_bid_and_fees_once(
    buy_fee_currency,
    sell_fee_currency,
):
    buy, sell = synthetic_small_route()
    buy = replace(
        buy,
        quote_currency="USDT",
        fx=synthetic_fx("USDT", "1.30", "1.40"),
        fee=Fee(D(".001"), buy_fee_currency, True),
    )
    sell = replace(
        sell,
        quote_currency="EUR",
        fx=synthetic_fx("EUR", "1.45", "1.55"),
        bids=levels((110, 20)),
        fee=Fee(D(".003"), sell_fee_currency, True),
    )
    result = evaluate_route(buy, sell, D(1), now=NOW, costs=NO_COST)
    with localcontext() as ctx:
        ctx.prec = 78
        assert abs(
            result.acquisition_cost_cad
            - result.buy_quote_required * D("1.40") / D(".998")
        ) < D("1e-70")
        assert result.sale_proceeds_cad == result.sell_quote_received * D("1.45") * D(
            ".998"
        )
        assert (
            result.net_profit_cad
            == result.sale_proceeds_cad - result.acquisition_cost_cad
        )
        assert result.gross_profit_cad - result.fees_cad == result.net_profit_cad
        assert (
            result.capital_requirements[buy.venue]["USDT"] == result.buy_quote_required
        )
        assert (
            result.capital_requirements[sell.venue]["SYNTHETIC-BTC"]
            == result.sell_base_required
        )


def test_reciprocal_fx_roundoff_does_not_falsely_remove_supported_target():
    buy, sell = synthetic_small_route()
    buy = replace(
        buy, quote_currency="EUR", fx=synthetic_fx("EUR", "1.53951", "1.5605")
    )
    sell = replace(sell, quote_currency="EUR", fx=buy.fx)
    result = size_diagnostics(pinned(buy, sell))
    assert all(row["supported"] for row in result["comparisons"][:6])
    assert not result["comparisons"][-1]["supported"]


def test_fx_depth_limits_target_without_claiming_stablecoin_parity():
    buy, sell = synthetic_small_route()
    buy = replace(
        buy, quote_currency="USDT", fx=synthetic_fx("USDT", "1.2", "1.4", depth="40")
    )
    value = size_diagnostics(pinned(buy, sell))
    assert value["comparisons"][0]["supported"]
    assert all(not row["supported"] for row in value["comparisons"][1:])
    assert all(
        row["estimated_net_profit_cad"] is None for row in value["comparisons"][1:]
    )


def test_multihop_reverse_costs_round_trip_fee_spread_cannot_create_money():
    fx = synthetic_fx("USDT", "1.30", "1.40", fee=".006")
    with localcontext() as ctx:
        ctx.prec = 78
        acquisition = required_input(fx.from_cad, D(10))
        assert acquisition > D(14)
        assert convert(fx.to_cad, D(10)) < D(13)
        assert convert(fx.to_cad, convert(fx.from_cad, D(100))) < D(100)
