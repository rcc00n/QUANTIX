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




































































