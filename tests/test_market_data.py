"""All markets, prices and venues in these tests are explicitly SYNTHETIC."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import ccxt
import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import ClockGuard, Schedule, retry_delay
from arbiq.connectors.ccxt_public import PublicConnector, classify_error
from arbiq.connectors.registry import available_candidates, capabilities, eligibility
from arbiq.market_data.cache import BookCache
from arbiq.market_data.discovery import normalize_market, selected_markets
from arbiq.market_data.identity import asset_identity
from arbiq.market_data.quality import DataState, fresh_view, validate_book
from arbiq.notifications.urgent import UrgencyPolicy
from pydantic import ValidationError


def synthetic_market(base="BTC", quote="USD", **changes):
    return {
        "id": f"{base}-{quote}",
        "symbol": f"{base}/{quote}",
        "base": base,
        "quote": quote,
        "spot": True,
        "contract": False,
        "active": True,
        "precision": {"amount": "0.0001", "price": "0.01"},
        "limits": {"cost": {"min": "1"}},
        **changes,
    }


def synthetic_raw(now=None):
    now = now or datetime.now(UTC)
    return {
        "symbol": "BTC/USD",
        "timestamp": int(now.timestamp() * 1000),
        "bids": [[str(100 - i), "2"] for i in range(10)],
        "asks": [[str(101 + i), "3"] for i in range(10)],
        "nonce": 123,
    }


def synthetic_capture(raw=None, **kwargs):
    now = kwargs.pop("received_at", datetime.now(UTC))
    return validate_book(
        synthetic_raw(now) if raw is None else raw,
        market_id=kwargs.pop("market_id", uuid4()),
        exchange=kwargs.pop("exchange", "synthetic-a"),
        symbol=kwargs.pop("symbol", "BTC/USD"),
        base_key=kwargs.pop("base_key", "asset:BTC"),
        quote_key=kwargs.pop("quote_key", "fiat:USD"),
        received_at=now,
        request_started_at=now - timedelta(milliseconds=20),
        latency_ms=20,
        config=kwargs.pop("config", CollectorConfig()),
        **kwargs,
    )


def test_identity_does_not_merge_wrappers_unknowns_or_stablecoins():
    assert asset_identity("synthetic-a", "BTC").key == asset_identity("synthetic-b", "BTC").key
    assert asset_identity("synthetic-a", "WBTC").key != asset_identity("synthetic-a", "BTC").key
    assert not asset_identity("synthetic-a", "BTC", {"name": "Wrapped Bitcoin"}).verified
    assert not asset_identity("synthetic-a", "SOL", {"name": "Solana Wormhole"}).verified
    assert (
        asset_identity("synthetic-a", "CUSTOM").key != asset_identity("synthetic-b", "CUSTOM").key
    )
    assert (
        len({asset_identity("synthetic-a", code).key for code in ("CAD", "USD", "USDC", "USDT")})
        == 4
    )
    assert asset_identity("synthetic-a", "USDC").kind == "stablecoin"


def test_discovery_shares_identities_only_within_its_snapshot():
    identity_by_symbol = {}
    normal = normalize_market(
        "synthetic", synthetic_market(), {}, identity_by_symbol=identity_by_symbol
    )
    same_base = normalize_market(
        "synthetic", synthetic_market(quote="CAD"), {}, identity_by_symbol=identity_by_symbol
    )
    rediscovered = normalize_market(
        "synthetic", synthetic_market(), {"BTC": {"name": "Wrapped Bitcoin"}}, identity_by_symbol={}
    )
    assert normal.base is same_base.base
    assert normal.base.verified and not rediscovered.base.verified
    assert normal.base.key != rediscovered.base.key


def test_dynamic_discovery_precision_filtering_and_base_diversity():
    markets = [
        normalize_market(
            "synthetic", synthetic_market(base, quote), {}, {"quoteVolume": "200000"}, 4
        )
        for base in ("BTC", "ETH", "SOL", "OTHER")
        for quote in ("CAD", "USD")
    ]
    assert all(m.price_tick == Decimal("0.01") for m in markets)
    config = CollectorConfig(max_markets_per_exchange=4)
    selected = selected_markets(markets, config)
    assert len({m.base.key for m in selected}) == 4
    assert all(m.quote.symbol == "CAD" for m in selected)
    assert any(m.base.symbol == "OTHER" for m in selected)  # Not a fixed seven-asset universe.
    filtered = selected_markets(markets, CollectorConfig(base_assets=["ETH"], quote_assets=["USD"]))
    assert [m.native_symbol for m in filtered] == ["ETH/USD"]
    assert not selected_markets(markets, CollectorConfig(minimum_quote_volume="999999"))
    assert not selected_markets(
        [replace(markets[0], active=None)], CollectorConfig(allow_unknown_active=False)
    )
    assert selected_markets(
        [replace(markets[0], active=None)], CollectorConfig(allow_unknown_active=True)
    )
    with pytest.raises(ValueError):
        normalize_market("synthetic", synthetic_market(spot=False), {})
    with pytest.raises(ValueError):
        normalize_market("synthetic", synthetic_market(contract=True), {})
    with pytest.raises(ValueError):
        normalize_market("synthetic", synthetic_market(base=None), {})
    places = normalize_market(
        "synthetic",
        synthetic_market(precision={"amount": 8, "price": 2}),
        {},
        precision_mode=2,
    )
    assert places.amount_step == Decimal("0.00000001")


def test_capability_and_eligibility_detection_never_infers_manual_eligibility():
    supported, missing = available_candidates(["kraken", "synthetic-absent", "ndax"], ["ndax"])
    assert supported == ["kraken"] and missing == ["synthetic-absent"]
    assert eligibility("kraken")["classification"] == "REGISTERED_ENTITY"
    assert eligibility("synthetic-venue")["classification"] == "UNVERIFIED"
    assert not eligibility("kraken")["manual_trading_eligible"]
    adapter = type(
        "SyntheticAdapter",
        (),
        {"id": "synthetic-venue", "has": {"spot": True, "fetchOrderBook": True}},
    )()
    caps = capabilities(adapter, [synthetic_market(quote="CAD")])
    assert caps["cad_market_available"] and caps["order_book_rest"]
    assert not caps["public_market_data_verified"]




















