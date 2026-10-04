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








