"""All prices, assets, times and fee evidence in this module are SYNTHETIC."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from itertools import product
from pathlib import Path

import pytest
from arbiq.market_data.contracts import BookLevel
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.analysis import dump_input, load_input, resize
from arbiq.opportunities.contracts import (
    ConversionLeg,
    ConversionRoute,
    Costs,
    Fee,
    FXConversion,
    MarketInput,
    MarketRules,
    TransferAssumptions,
    identity_cad,
)
from arbiq.opportunities.math import (
    InsufficientDepth,
    conversion_capacity,
    conversion_leg_from_book,
    convert,
    evaluate_route,
    maximum_quantity,
    optimize_route,
    required_input,
    transfer_scenario,
)
from arbiq.opportunities.replay import ReplayBook, main, replay_document, replay_route

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)
NO_COST = Costs(D(0), D(0), D(0), True, "SYNTHETIC zero reserve assumption")
RULES = MarketRules(D("0.01"), D("0.01"), D(1), verified=True)
NO_FEE = Fee(D(0), verified=True, source="SYNTHETIC fee schedule")


def levels(*items):
    return tuple(
        BookLevel(price=str(price), quantity=str(quantity)) for price, quantity in items
    )


def markets():
    buy = MarketInput(
        "synthetic-buy",
        "venue-a",
        "BTC",
        "CAD",
        levels((100, 1), (110, 2)),
        levels((99, 5)),
        NO_FEE,
        RULES,
        identity_cad(),
    )
    sell = MarketInput(
        "synthetic-sell",
        "venue-b",
        "BTC",
        "CAD",
        levels((131, 5)),
        levels((130, 1), (115, 2)),
        NO_FEE,
        RULES,
        identity_cad(),
    )
    return buy, sell


def run(buy, sell, quantity="1", **kwargs):
    return evaluate_route(buy, sell, D(quantity), now=NOW, costs=NO_COST, **kwargs)


def fx(currency="USDT", bid="1.30", ask="1.40", fee="0", quantity="100000"):
    bids, asks = levels((bid, quantity)), levels((ask, quantity))
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
        "SYNTHETIC executable stablecoin/CAD depth",
    )


def test_multilevel_asks_and_bids_weighted_execution():
    buy, sell = markets()
    result = run(buy, sell, "2")
    assert result.buy_notional == D(210)
    assert result.sell_notional == D(245)
    assert result.buy_vwap == D(105)
    assert result.sell_vwap == D("122.5")
    assert result.net_profit_cad == D(35)
    with localcontext() as context:
        context.prec = 78
        assert result.net_return_pct == D(35) / D(210) * D(100)
    assert result.verified
    assert result.mode == "PRE_FUNDED"


@pytest.mark.parametrize("quantity", ["3.01", "10", "1000000"])
def test_impossible_depth_cannot_create_executable_quantity(quantity):
    buy, sell = markets()
    with pytest.raises(InsufficientDepth):
        run(buy, sell, quantity)


def test_partial_available_depth_caps_optimizer_not_fabricated_fills():
    buy, sell = markets()
    sell = replace(sell, bids=levels((130, "0.123")))
    assert maximum_quantity(buy, sell) == D("0.12")
    result = optimize_route(
        buy, sell, budget_cad=D(1000), now=NOW, costs=NO_COST, budgets=()
    )
    assert result.maximum.buy_quantity == D("0.12")
    assert result.best.sell_quantity <= D("0.123")


def test_step_rounding_and_residual_not_monetized():
    buy, sell = markets()
    buy = replace(buy, rules=replace(RULES, amount_step=D("0.03")))
    sell = replace(sell, rules=replace(RULES, amount_step=D("0.05")))
    result = run(buy, sell, "0.299")
    assert result.buy_quantity == D("0.27")
    assert result.sell_quantity == D("0.25")
    assert result.residual_base == D("0.02")
    assert result.net_profit_cad == D("5.50")


def test_observed_prices_must_align_with_known_exchange_tick():
    buy, _ = markets()
    assert replace(buy, rules=replace(RULES, price_tick=D("0.01")))
    with pytest.raises(ValueError, match="price precision"):
        replace(buy, rules=replace(RULES, price_tick=D("0.03")))


@pytest.mark.parametrize(
    "rules,message",
    [
        (replace(RULES, min_quantity=D(2)), "BELOW_MINIMUM_QUANTITY"),
        (replace(RULES, min_notional=D(300)), "BELOW_MINIMUM_NOTIONAL"),
        (replace(RULES, max_quantity=D("0.5")), "ABOVE_MAXIMUM_QUANTITY"),
        (replace(RULES, max_notional=D(50)), "ABOVE_MAXIMUM_NOTIONAL"),
    ],
)
def test_order_limits_checked_on_each_leg(rules, message):
    buy, sell = markets()
    with pytest.raises(ValueError, match=message):
        run(replace(buy, rules=rules), sell)
    with pytest.raises(ValueError, match=message):
        run(buy, replace(sell, rules=rules))


def test_quote_fees_included_once_in_cashflow_and_capital():
    buy, sell = markets()
    buy = replace(buy, fee=Fee(D("0.01"), verified=True))
    sell = replace(sell, fee=Fee(D("0.02"), verified=True))
    result = run(buy, sell)
    assert result.buy_fee_amount == D(1)
    assert result.sell_fee_amount == D("2.60")
    assert result.buy_quote_required == D(101)
    assert result.sale_proceeds_cad == D("127.40")
    assert result.fees_cad == D("3.60")
    assert result.net_profit_cad == D("26.40")
    assert result.capital_requirements == {
        "venue-a": {"CAD": D(101)},
        "venue-b": {"BTC": D(1)},
    }


def test_base_fees_reduce_acquisition_and_increase_seller_inventory():
    buy, sell = markets()
    buy = replace(buy, fee=Fee(D("0.1"), "base", True))
    sell = replace(sell, fee=Fee(D("0.1"), "base", True))
    result = run(buy, sell)
    assert result.base_acquired == D("0.9")
    assert result.sell_quantity == D("0.81")
    assert result.sell_base_required == D("0.891")
    assert result.residual_base == D("0.009")
    assert result.net_profit_cad == D("5.30")
    assert result.fees_cad == D("20.530")
    assert result.base_fee_value_cad == D("20.530")
    assert result.net_profit_cad == result.gross_profit_cad - result.fees_cad


def test_marketable_execution_rejects_maker_fee_claim():
    with pytest.raises(ValueError, match="taker"):
        Fee(D("0.001"), execution="maker")


def test_unknown_fees_and_limits_never_become_verified_or_zero_by_default():
    buy, sell = markets()
    buy = replace(buy, fee=Fee(D("0.006")), rules=MarketRules())
    result = run(buy, sell)
    assert result.buy_fee_amount == D("0.600")
    assert not result.verified
    assert "BUY_FEE_UNVERIFIED" in result.reasons
    assert "BUY_MARKET_LIMITS_UNVERIFIED" in result.reasons


def test_reserve_and_rebalancing_costs_and_inventory_projection():
    buy, sell = markets()
    costs = Costs(D(10), D(20), D(2), True)
    result = evaluate_route(buy, sell, D(1), now=NOW, costs=costs)
    assert result.slippage_reserve_cad == D("0.1")
    assert result.rebalancing_cost_cad == D("2.2")
    assert result.net_profit_cad == D("27.7")
    assert result.inventory_projection["venue-a"] == {"CAD": D(-100), "BTC": D(1)}
    assert result.inventory_projection["venue-b"] == {"BTC": D(-1), "CAD": D(130)}


@pytest.mark.parametrize("bid,expected", [("1.30", "69"), ("0.50", "-35")])
def test_stablecoin_depeg_and_directional_conversion(bid, expected):
    buy, sell = markets()
    sell = replace(sell, quote_currency="USDT", fx=fx(bid=bid))
    result = run(buy, sell)
    assert result.net_profit_cad == D(expected)
    assert result.sale_proceeds_cad == D(130) * D(bid)
    assert result.net_profit_quote is None


def test_fx_acquisition_uses_ask_and_liquidation_uses_bid():
    conversion = fx()
    assert convert(conversion.to_cad, D(10)) == D(13)
    assert required_input(conversion.from_cad, D(10)) == D(14)
    assert conversion_capacity(conversion.to_cad) == D(100000)
    assert conversion_capacity(conversion.from_cad) == D(140000)


def test_fx_depth_multiple_levels_and_fees_are_not_double_counted():
    forward = ConversionLeg("USD", "CAD", levels(("1.4", 10), ("1.3", 20)), D("0.01"))
    route = ConversionRoute((forward,))
    assert convert(route, D(20)) == D("26.73")
    assert required_input(route, D("26.73")) == D(20)
    with pytest.raises(InsufficientDepth):
        convert(route, D(31))


def test_multihop_conversion_and_capacity():
    route = ConversionRoute(
        (
            ConversionLeg("USDT", "USD", levels(("0.98", 100))),
            ConversionLeg("USD", "CAD", levels(("1.4", 49))),
        )
    )
    assert conversion_capacity(route) == D(50)
    assert convert(route, D(50)) == D("68.6")
    assert required_input(route, D("68.6")) == D(50)
    with pytest.raises(InsufficientDepth):
        convert(route, D(51))


@pytest.mark.parametrize(
    "conversion,reason",
    [
        (None, "SELL_CAD_CONVERSION_UNAVAILABLE"),
        (
            replace(fx(), fresh_until=NOW - timedelta(seconds=1)),
            "SELL_CAD_CONVERSION_STALE",
        ),
    ],
)
def test_missing_or_stale_fx_suppresses_cad_profit(conversion, reason):
    buy, sell = markets()
    sell = replace(sell, quote_currency="USDT", fx=conversion)
    result = run(buy, sell)
    assert result.net_profit_cad is None
    assert not result.verified
    assert reason in result.reasons


def test_unverified_fx_is_labeled_and_insufficient_fx_depth_is_not_extrapolated():
    buy, sell = markets()
    conversion = replace(fx(), verified=False)
    result = run(buy, replace(sell, quote_currency="USDT", fx=conversion))
    assert result.net_profit_cad is not None
    assert not result.verified
    assert "SELL_CAD_CONVERSION_UNVERIFIED" in result.reasons
    result = run(buy, replace(sell, quote_currency="USDT", fx=fx(quantity="1")))
    assert result.net_profit_cad is None
    assert "INSUFFICIENT_EXECUTABLE_FX_DEPTH" in result.reasons


def test_same_prices_and_positive_raw_spread_can_be_negative_after_fees():
    buy, sell = markets()
    sell = replace(
        sell,
        asks=levels((102, 5)),
        bids=levels((100, 5)),
        fee=Fee(D("0.01"), verified=True),
    )
    assert run(buy, sell).net_profit_cad == -1
    sell = replace(sell, bids=levels(("100.5", 5)))
    assert run(buy, sell).net_profit_cad == D("-0.505")


def test_extreme_genuine_anomaly_retained_but_tiny_depth_is_not_large_profit():
    buy, sell = markets()
    buy = replace(buy, asks=levels((50, "0.04")), bids=levels((49, 1)))
    result = optimize_route(
        buy, sell, budget_cad=D(10000), now=NOW, costs=NO_COST, budgets=()
    )
    assert result.best.buy_notional == D(2)
    assert result.best.net_profit_cad == D("3.2")
    assert result.maximum.buy_quantity == D("0.04")


def test_optimizer_prefers_profitable_small_size_over_negative_deeper_size():
    buy, sell = markets()
    buy = replace(buy, asks=levels((100, 1), (150, 2)))
    result = optimize_route(
        buy,
        sell,
        budget_cad=D(1000),
        now=NOW,
        costs=NO_COST,
        budgets=(D(50), D(100), D(250)),
    )
    assert result.best.buy_quantity == 1
    assert result.best.net_profit_cad == 30
    assert result.maximum.net_profit_cad < 0
    assert result.budgets[1].best.buy_quantity == D("0.50")
    assert result.budgets[2].best.buy_quantity == 1


def test_budget_fee_inclusive_and_custom_budget_exact():
    buy, sell = markets()
    buy = replace(buy, fee=Fee(D("0.01"), verified=True))
    result = optimize_route(
        buy, sell, budget_cad=D("25.50"), now=NOW, costs=NO_COST, budgets=()
    )
    assert result.best.buy_quantity == D("0.25")
    assert result.best.acquisition_cost_cad == D("25.25")
    assert result.best.acquisition_cost_cad <= D("25.50")


def test_fee_negative_routes_are_proved_without_enumerating_thousands_of_sizes():
    buy, sell = markets()
    buy = replace(buy, fee=Fee(D("0.006"), verified=True))
    sell = replace(
        sell,
        bids=levels(("100.1", 10)),
        asks=levels((101, 10)),
        fee=Fee(D("0.006"), verified=True),
    )
    result = optimize_route(buy, sell, budget_cad=D(1000), now=NOW, costs=NO_COST)
    assert result.best.buy_quantity == D("0.01")
    assert result.best.net_profit_cad < 0
    assert (
        result.evaluated_count < 50
    )  # All seven budgets, deterministic CPU-work assertion.
    assert "certified" in result.optimality


def test_many_depth_levels_do_not_increase_work_for_proven_fee_negative_route():
    buy, sell = markets()
    buy = replace(
        buy,
        asks=levels(*((100 + index, 1) for index in range(25))),
        fee=Fee(D("0.006"), verified=True),
    )
    sell = replace(
        sell,
        bids=levels(*((D("100.1") - D("0.01") * index, 1) for index in range(25))),
        asks=levels((101, 30)),
        fee=Fee(D("0.006"), verified=True),
    )
    result = optimize_route(buy, sell, budget_cad=D(1000), now=NOW, costs=NO_COST)
    assert result.best.buy_quantity == D("0.01")
    assert (
        result.evaluated_count <= 21
    )  # Two/three minimum candidates for seven budgets.
    assert "certified" in result.optimality


def test_loss_fast_path_preserves_better_inventory_alignment_over_first_feasible_size():
    buy, sell = markets()
    buy = replace(
        buy,
        rules=replace(RULES, amount_step=D("0.03")),
        fee=Fee(D("0.006"), verified=True),
    )
    sell = replace(
        sell,
        bids=levels(("100.1", 10)),
        asks=levels((101, 10)),
        rules=replace(RULES, amount_step=D("0.05")),
        fee=Fee(D("0.006"), verified=True),
    )
    result = optimize_route(
        buy, sell, budget_cad=D(1000), now=NOW, costs=NO_COST, budgets=()
    )
    first = run(buy, sell, "0.06")
    assert result.best.buy_quantity == D("0.15")
    assert result.best.net_profit_cad > first.net_profit_cad
    oracle = max(
        (run(buy, sell, str(D("0.03") * index)) for index in range(2, 101)),
        key=lambda value: (value.net_profit_cad, -value.acquisition_cost_cad),
    )
    assert result.best == oracle
    assert result.evaluated_count < 40


def test_optimizer_caps_size_by_executable_cad_conversion_depth():
    buy, sell = markets()
    buy = replace(buy, rules=replace(RULES, amount_step=D("0.0001")))
    sell = replace(
        sell,
        quote_currency="USDT",
        fx=fx(quantity="10"),
        rules=replace(RULES, amount_step=D("0.0001")),
    )
    result = optimize_route(
        buy, sell, budget_cad=D(1000), now=NOW, costs=NO_COST, budgets=()
    )
    assert result.best is not None
    assert result.best.sell_quote_received <= D(10)
    assert result.best.buy_quantity == D("0.0769")


def test_optimizer_respects_max_quantity_and_max_notional():
    buy, sell = markets()
    buy = replace(buy, rules=replace(RULES, max_notional=D(150)))
    sell = replace(sell, rules=replace(RULES, max_quantity=D("1.2")))
    result = optimize_route(
        buy, sell, budget_cad=D(1000), now=NOW, costs=NO_COST, budgets=()
    )
    assert result.maximum.buy_quantity == D("1.2")
    assert result.best.buy_quantity <= D("1.2")


def test_optimizer_large_lattice_certifies_without_enumerating_millions_of_ticks():
    buy, sell = markets()
    buy = replace(buy, rules=replace(RULES, amount_step=D("0.00000001")))
    sell = replace(sell, rules=replace(RULES, amount_step=D("0.00000001")))
    result = optimize_route(
        buy, sell, budget_cad=D(320), now=NOW, costs=NO_COST, budgets=()
    )
    assert result.best.buy_quantity == 3
    assert result.evaluated_count < 100
    assert "certified" in result.optimality










































