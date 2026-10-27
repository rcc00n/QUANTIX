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


























