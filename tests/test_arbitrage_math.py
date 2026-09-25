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






























































































