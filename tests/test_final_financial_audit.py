"""Financial audit regressions. Every market, price and portfolio here is SYNTHETIC."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from arbiq.opportunities.analysis import dump_input
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
from arbiq.settings.strategy import (
    PaperStrategy,
    minimum_buy_satisfied,
    read_pinned_strategy,
)
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
    assert len(result["comparisons"]) == 5
    for row in result["comparisons"]:
        assert row["supported"]
        assert D(row["buy_capital_cad"]) == D(row["requested_buy_capital_cad"])
        assert D(row["estimated_net_profit_cad"]) < 0
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








