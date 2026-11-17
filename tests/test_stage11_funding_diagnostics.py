"""All prices, holdings and market timestamps below are SYNTHETIC."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from arbiq.paper_trading.funding_sizing import (
    InventoryBalance,
    inventory_liquidation_mark,
    prefunded_capacity,
    prefunded_net_return,
    read_pair,
)
from arbiq.settings.strategy import presets
from test_stage8b_planner import AT, pinned

D = Decimal


def test_spendable_units_preserve_all_numeric_digits():
    balance = InventoryBalance(
        D("99999999999999999999.123456789012345678"), D("0.000000000000000001")
    )
    assert balance.spendable == D("99999999999999999999.123456789012345677")


def test_market_capacity_exceeds_available_size_for_one_sided_inventory():
    pair = read_pair(pinned(), AT, presets()["Aggressive Research"])
    market, funded = prefunded_capacity(
        pair, D(500), InventoryBalance(D(500)), InventoryBalance(D(1), D(1))
    )
    assert market and market.acquisition_cad > D(50) and funded is None
    assert market.buy_quote <= D(500)
    _, tiny = prefunded_capacity(
        pair, D(500), InventoryBalance(D(525), D(500)), InventoryBalance(D(10))
    )
    assert tiny and D(0) < tiny.acquisition_cad < D(50)


def test_two_sided_inventory_return_uses_pinned_full_size_marks():
    pair = read_pair(pinned(), AT, presets()["Aggressive Research"])
    market, funded = prefunded_capacity(
        pair, D(500), InventoryBalance(D(500)), InventoryBalance(D(50))
    )
    assert funded and market is funded
    value = inventory_liquidation_mark(pair, funded.buy_quote, funded.sell_base)
    assert value and value > funded.acquisition_cad
    with localcontext() as ctx:
        ctx.prec = 78
        assert (
            prefunded_net_return(funded, value)
            == funded.evaluation.net_profit_cad * 100 / value
        )
    assert prefunded_net_return(funded, None) is None
    assert (
        inventory_liquidation_mark(
            replace(pair, sell=replace(pair.sell, fx=None)), D(10), D(1)
        )
        is None
    )
    assert inventory_liquidation_mark(pair, D(10), D("1e18")) is None


def test_funding_refuses_independently_mismatched_source_times():
    evaluation = pinned()
    evaluation.evidence["sell_book"]["exchange_timestamp"] = (
        AT - timedelta(seconds=11)
    ).isoformat()
    with pytest.raises(ValueError, match="CROSS_BOOK_SOURCE_SKEW"):
        read_pair(evaluation, AT, presets()["Aggressive Research"])
