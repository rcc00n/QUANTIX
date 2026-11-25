"""Synthetic instrument metadata and market inputs; no exchange orders or app data."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arbiq.finance.fees import assess_fee
from arbiq.market_data.contracts import BookLevel
from arbiq.market_data.discovery import normalize_market
from arbiq.opportunities.analysis import dump_input, load_input
from arbiq.opportunities.contracts import Fee, MarketInput, MarketRules, identity_cad
from arbiq.opportunities.engine import ArbitrageEngine
from arbiq.opportunities.math import evaluate_route, optimize_route
from arbiq.paper_trading.coordinator import PaperCoordinator
from arbiq.paper_trading.execution import simulate_leg

D = Decimal
NOW = datetime(2026, 11, 22, tzinfo=UTC)


def market(exchange, info, **data):
    return normalize_market(
        exchange,
        {
            "id": "BTC-CAD",
            "symbol": "BTC/CAD",
            "base": "BTC",
            "quote": "CAD",
            "spot": True,
            "active": True,
            "precision": {"amount": "0.001", "price": "0.01"},
            "limits": {"cost": {"min": "1"}, "amount": {"min": "0.001"}},
            "info": info,
            **data,
        },
        {},
        precision_mode=4,
        rules_observed_at=NOW,
    )


def test_kraken_preserves_native_base_price_and_quote_units():
    record = market(
        "kraken",
        {
            "status": "online",
            "lot_decimals": 8,
            "tick_size": "0.10000",
            "ordermin": "0.00005",
            "costmin": "0.5",
            "cost_decimals": "5",
        },
    )
    assert record.amount_step == D("0.00000001")
    assert str(record.price_tick) == "0.10000"
    assert record.min_notional == D("0.5")
    assert record.metadata["minimum_quantity"] == "0.00005"
    evidence = record.metadata["trading_rules"]
    assert evidence["market_order_available"] is True
    assert evidence["cost_step"] == "0.00001"
    assert evidence["observed_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    "flag", ("cancel_only", "limit_only", "post_only", "auction_mode")
)
def test_coinbase_restrictions_override_online_status(flag):
    flags = dict.fromkeys(
        ("cancel_only", "limit_only", "post_only", "auction_mode"), False
    )
    flags[flag] = True
    record = market(
        "coinbaseexchange",
        {**flags, "status": "online", "base_increment": "1e-8", "fx_stablecoin": False},
    )
    assert record.amount_step == D("1e-8")
    assert record.metadata["fx_stablecoin"] is False
    assert record.metadata["trading_rules"]["market_order_available"] is False
    assert record.metadata["trading_rules"]["status_flags"] == [flag]
    assert record.min_notional == D("1")  # Legacy conservative research floor.


def test_unknown_flags_and_float_metadata_cannot_establish_exact_native_rules():
    record = market(
        "coinbaseexchange",
        {"status": "online", "base_increment": 0.00000001, "limit_only": "false"},
    )
    assert record.amount_step == D("0.001")
    assert record.metadata["trading_rules"]["market_order_available"] is None
    assert (
        market("synthetic", {}, active=False).metadata["trading_rules"][
            "market_order_available"
        ]
        is False
    )


def test_advanced_retains_published_base_and_quote_limits_independently():
    record = market(
        "coinbase",
        {
            "product_type": "SPOT",
            "status": "online",
            "base_increment": "0.00000001",
            "price_increment": "0.1",
            "quote_increment": "0.01",
            "base_min_size": "0.0001",
            "base_max_size": "100",
            "quote_min_size": "1.25",
            "quote_max_size": "50000",
            **dict.fromkeys(
                (
                    "cancel_only",
                    "post_only",
                    "limit_only",
                    "auction_mode",
                    "is_disabled",
                    "view_only",
                    "trading_disabled",
                ),
                False,
            ),
        },
    )
    assert record.metadata["trading_rules"]["market_order_available"] is True
    assert record.amount_step == D("0.00000001")
    assert record.price_tick == D("0.1")
    assert record.metadata["trading_rules"]["cost_step"] == "0.01"
    assert record.metadata["minimum_quantity"] == "0.0001"
    assert record.metadata["maximum_quantity"] == "100"
    assert record.min_notional == D("1.25")
    assert record.metadata["maximum_notional"] == "50000"


def test_okx_retains_maximum_units_without_calling_them_native_quote():
    record = market(
        "okx",
        {
            "instType": "SPOT",
            "state": "live",
            "lotSz": "0.00000001",
            "minSz": "0.00001",
            "tickSz": "0.1",
            "maxMktSz": "1000001",
            "maxMktAmt": "1000002",
        },
    )
    rules = record.metadata["trading_rules"]
    assert rules["market_order_available"] is True
    assert rules["maxMktSz_USDT"] == "1000001"
    assert rules["maxMktAmt_USD"] == "1000002"
    assert record.metadata["maximum_notional"] is None


def inputs():
    restrictions = MarketRules(
        D("0.01"), D("0.01"), D("1"), verified=True, price_tick=D("0.1")
    )
    buy = MarketInput(
        "synthetic-buy",
        "synthetic-buy",
        "asset:BTC",
        "CAD",
        (BookLevel(price="10.0", quantity="10"),),
        (BookLevel(price="9.9", quantity="10"),),
        Fee(D("0.001")),
        restrictions,
        identity_cad(),
    )
    sell = replace(
        buy,
        market_id="synthetic-sell",
        venue="synthetic-sell",
        asks=(BookLevel(price="11.2", quantity="10"),),
        bids=(BookLevel(price="11.0", quantity="10"),),
    )
    return buy, sell


def test_rules_are_pinned_and_legacy_serialized_rules_replay_with_original_math():
    buy, sell = inputs()
    legacy = dump_input(buy)
    for field in as_new_rules():
        del legacy["rules"][field]
    assert load_input(legacy) == buy
    original = evaluate_route(buy, sell, D("3.42"), now=NOW)
    assert evaluate_route(load_input(legacy), sell, D("3.42"), now=NOW) == original
    new = replace(buy, rules=replace(buy.rules, **as_new_rules()))
    assert load_input(dump_input(new)) == new
    projected = evaluate_route(new, sell, D("3.42"), now=NOW)
    assert (
        projected == original
    )  # Unverified quote/fee precision never rewrites cash flows.


def as_new_rules():
    return {
        "market_order_available": True,
        "version": "public-spot-rules-v1",
        "observed_at": NOW,
        "cost_step": D("0.001"),
        "native_status": "online",
        "restrictions": (),
        "limitations": ("SYNTHETIC_TEST",),
        "external_notional_limits": (("maxMktAmt_USD", D("10000")),),
    }


def test_research_projection_retains_market_order_rejection_and_refuses_future_rules():
    buy, sell = inputs()
    blocked = replace(buy, rules=replace(buy.rules, market_order_available=False))
    answer = optimize_route(blocked, sell, budget_cad=D(100), now=NOW, budgets=(D(50),))
    assert answer.best is answer.maximum is None
    assert answer.evaluated_count == 0
    assert answer.reasons == ("BUY_MARKET_ORDER_UNAVAILABLE",)
    assert {row.reason for row in answer.budgets} == {"BUY_MARKET_ORDER_UNAVAILABLE"}
    with pytest.raises(ValueError, match="BUY_MARKET_ORDER_UNAVAILABLE"):
        evaluate_route(blocked, sell, D(1), now=NOW)
    unavailable = replace(
        sell, rules=replace(sell.rules, observed_at=NOW + timedelta(seconds=1))
    )
    answer = optimize_route(buy, unavailable, budget_cad=D(100), now=NOW)
    assert answer.reasons == ("SELL_MARKET_RULES_NOT_YET_AVAILABLE",)


def test_unknown_current_availability_remains_an_explicit_research_limitation():
    buy, sell = inputs()
    unknown = replace(buy, rules=replace(buy.rules, version="public-spot-rules-v1"))
    answer = evaluate_route(unknown, sell, D(1), now=NOW)
    assert "BUY_MARKET_ORDER_AVAILABILITY_UNVERIFIED" in answer.reasons
    assert not answer.verified


@pytest.mark.parametrize(
    ("rules", "reason"),
    (
        (MarketRules(market_order_available=False), "MARKET_ORDER_UNAVAILABLE"),
        (
            MarketRules(observed_at=NOW + timedelta(seconds=1)),
            "MARKET_RULES_NOT_YET_AVAILABLE",
        ),
    ),
)
def test_paper_does_not_fill_a_native_market_restriction(rules, reason):
    minimal_book = SimpleNamespace(received_at=NOW)
    with pytest.raises(ValueError, match=reason):
        simulate_leg(minimal_book, "buy", D(1), D(100), Fee(D("0.001")), rules)


def test_pending_paper_order_pins_only_causal_market_restriction():
    market_id, other_market_id = uuid4(), uuid4()
    blocked = market("kraken", {"status": "maintenance"})
    coordinator = SimpleNamespace(
        collector=SimpleNamespace(
            scanner=SimpleNamespace(
                market_map={market_id: (None, blocked)},
            )
        )
    )
    capture = PaperCoordinator.market_order_restrictions
    assert (
        capture(coordinator, (market_id, other_market_id), NOW - timedelta(seconds=1))
        == {}
    )
    evidence = capture(coordinator, (market_id, other_market_id), NOW)
    assert evidence[str(market_id)]["native_status"] == "maintenance"
    assert evidence[str(market_id)]["observed_at"] == NOW.isoformat()


def test_native_metadata_flows_into_scanner_market_input():
    record = market(
        "kraken",
        {
            "status": "limit_only",
            "lot_decimals": 8,
            "tick_size": "0.1",
            "ordermin": "0.0001",
            "costmin": "0.5",
            "cost_decimals": "4",
        },
    )
    side, _ = inputs()
    captured = SimpleNamespace(received_at=NOW, normalized=side)
    value = ArbitrageEngine.market_input(
        SimpleNamespace(slug="kraken"),
        record,
        captured,
        {"CAD": identity_cad()},
        fee=assess_fee(
            "kraken",
            "BTC",
            "CAD",
            configured_taker_floor=D("0.001"),
            advertised_taker=None,
            now=NOW,
        ),
        side="buy",
    )
    assert value.rules.market_order_available is False
    assert value.rules.native_status == "limit_only"
    assert value.rules.cost_step == D("0.0001")
    assert not value.rules.verified
    assert load_input(dump_input(value)) == value
