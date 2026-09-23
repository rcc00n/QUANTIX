"""SYNTHETIC books, identities and runtimes; never imply live-market verification."""

import asyncio
import hashlib
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.market_data.discovery import normalize_market
from arbiq.market_data.quality import DataState, fresh_view, safe_json, validate_book
from arbiq.opportunities.config import EngineConfig
from arbiq.opportunities.contracts import identity_cad
from arbiq.opportunities.engine import ArbitrageEngine

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class SyntheticCache:
    def __init__(self, books):
        self.books = {book.market_id: book for book in books}

    def get(self, market_id, *, now):
        book = self.books.get(market_id)
        return fresh_view(book, now=now, maximum_age_seconds=60) if book else None


class SyntheticStore:
    def __init__(self):
        self.saved = []
        self.transition = AsyncMock()
        self.expire_previous_runs = AsyncMock()

    async def save(self, key, payload, buy, sell, *args):
        body = safe_json({**payload, "id": str(uuid4()), "updated_at": NOW, "detected_at": NOW})
        self.saved.append((key, body, buy, sell))
        return body


def market(slug, base="BTC", quote="CAD", **metadata):
    raw = {
        "id": f"synthetic-{base}-{quote}",
        "symbol": f"{base}/{quote}",
        "base": base,
        "quote": quote,
        "spot": True,
        "contract": False,
        "active": True,
        "precision": {"amount": "0.001", "price": "0.01"},
        "limits": {"amount": {"min": "0.001"}, "cost": {"min": "1"}},
    }
    value = normalize_market(slug, raw, {}, precision_mode=4)
    return replace(value, id=uuid4(), metadata={**value.metadata, **metadata})


def capture(slug, discovered, *, ask="100", bid="99", received_at=NOW, quantities=None):
    quantities = quantities or ["10", "10", "10"]
    raw = {
        "symbol": discovered.native_symbol,
        "timestamp": int(received_at.timestamp()) * 1000,
        "bids": [[str(Decimal(bid) - i), quantities[i]] for i in range(3)],
        "asks": [[str(Decimal(ask) + i), quantities[i]] for i in range(3)],
    }
    return validate_book(
        raw,
        market_id=discovered.id,
        exchange=slug,
        symbol=discovered.native_symbol,
        base_key=discovered.base.key,
        quote_key=discovered.quote.key,
        received_at=received_at,
        request_started_at=received_at - timedelta(milliseconds=20),
        latency_ms=20,
        config=CollectorConfig(),
    )


def engine_fixture(*, buy_slug="synthetic-buy", sell_slug="synthetic-sell", config=None):
    buy, sell = market(buy_slug), market(sell_slug)
    books = [capture(buy_slug, buy), capture(sell_slug, sell, ask="111", bid="110")]
    runtimes = {
        slug: SimpleNamespace(
            slug=slug,
            markets=[m],
            catalog=[m],
            exchange_id=uuid4(),
            health={"error_code": None},
        )
        for slug, m in ((buy_slug, buy), (sell_slug, sell))
    }
    requested = set()

    def confirm(market_id):
        if market_id in requested:
            return "COOLDOWN"
        requested.add(market_id)
        return "QUEUED"

    collector = SimpleNamespace(
        settings=SimpleNamespace(arbitrage=config or EngineConfig()),
        store=SimpleNamespace(sessions=None, run_id=uuid4()),
        runtimes=runtimes,
        cache=SyntheticCache(books),
        request_confirmation=confirm,
        utc_clock=SimpleNamespace(snapshot=lambda: {"trusted": True, "host_healthy": True}),
        utcnow=lambda: NOW,
        stop=asyncio.Event(),
    )
    engine = ArbitrageEngine(collector)
    engine.store = SyntheticStore()
    engine.rebuild()
    return engine, buy, sell, requested


@pytest.mark.asyncio
async def test_exact_real_book_path_retains_evidence_and_unverified_conditions():
    engine, buy, sell, requests = engine_fixture()
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    _, body, buy_book, sell_book = engine.store.saved[0]
    assert body["status"] == "RESEARCH_ONLY"
    assert body["actionable"] is False
    assert body["eligibility"] == "UNVERIFIED"
    assert Decimal(body["quantity"]) > 0
    assert Decimal(body["net_profit_cad"]) > 0
    assert "CANADIAN_PROVINCIAL_ACCOUNT_ELIGIBILITY_UNVERIFIED" in body["reasons"]
    assert "PRE_FUNDED_INVENTORY_ASSUMED_NOT_VERIFIED" in body["reasons"]
    assert buy_book.market_id == buy.id and sell_book.market_id == sell.id
    assert requests == {buy.id, sell.id}
    assert engine.metrics["detected"] == 1 and engine.metrics["rechecked"] == 2




































