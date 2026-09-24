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


@pytest.mark.asyncio
async def test_same_economic_inputs_reuse_math_and_publish_new_observation():
    engine, buy, sell, _ = engine_fixture()
    fx = {"CAD": identity_cad()}
    await engine.evaluate_pair(buy.id, sell.id, fx, NOW)
    key, first, _, _ = engine.store.saved[0]

    decision = NOW + timedelta(seconds=1)
    engine.collector.utcnow = lambda: decision
    engine.collector.cache.books[buy.id] = capture("synthetic-buy", buy, received_at=decision)
    engine.collector.cache.books[sell.id] = capture(
        "synthetic-sell", sell, ask="111", bid="110", received_at=decision
    )
    engine.last_write[key] = time.monotonic() - engine.config.persistence_interval_seconds - 1
    await engine.evaluate_pair(buy.id, sell.id, fx, decision)
    _, second, buy_book, sell_book = engine.store.saved[1]
    assert engine.metrics["optimizer_fresh_calculations"] == 1
    assert engine.metrics["optimizer_exact_reuses"] == 1
    assert engine.metrics["evaluations"] == 2
    assert buy_book.received_at == sell_book.received_at == decision
    assert second["supporting_received_at"] == decision.isoformat()
    assert second["evidence"]["timing"]["evaluated_at"] == decision.isoformat()
    for field in ("costs", "budget_results", "maximum_executable", "optimizer"):
        assert first[field] == second[field]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ("expired_fx", "BUY_CAD_CONVERSION_STALE"),
        ("future_fx", "BUY_CAD_CONVERSION_NOT_YET_AVAILABLE"),
        ("depth_scale", None),
        ("changed_budget", None),
    ],
)
async def test_optimizer_reuse_respects_fx_time_and_exact_economic_inputs(update, reason):
    engine, buy, sell, _ = engine_fixture()
    original_fx = identity_cad()
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": original_fx}, NOW)
    fx = original_fx
    if update == "expired_fx":
        fx = replace(fx, fresh_until=NOW - timedelta(microseconds=1))
    elif update == "future_fx":
        fx = replace(fx, available_at=NOW + timedelta(microseconds=1))
    elif update == "depth_scale":
        engine.collector.cache.books[buy.id] = capture(
            "synthetic-buy", buy, quantities=["10.0", "10.0", "10.0"]
        )
    elif update == "changed_budget":
        engine.config = engine.config.model_copy(update={"default_budget_cad": Decimal("200")})
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": fx}, NOW)
    assert engine.metrics["optimizer_fresh_calculations"] == 2
    assert engine.metrics["optimizer_exact_reuses"] == 0
    if reason:
        assert reason in next(iter(engine.current.values()))["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clock",
    [
        None,
        {"trusted": False, "host_healthy": False},
        {"trusted": True, "host_healthy": False},
    ],
)
async def test_unhealthy_or_unknown_clock_never_produces_verified_opportunity(clock):
    engine, buy, sell, _ = engine_fixture()
    engine.collector.utc_clock = SimpleNamespace(snapshot=lambda: clock) if clock else None
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    body = engine.store.saved[0][1]
    assert body["status"] != "VERIFIED_MARKET" and body["actionable"] is False
    assert "HOST_CLOCK_UNHEALTHY" in body["reasons"]
    if clock is None or not clock["trusted"]:
        assert "UTC_CLOCK_UNTRUSTED" in body["reasons"]


@pytest.mark.asyncio
async def test_stale_book_is_invalidated_and_never_verified():
    engine, buy, sell, _ = engine_fixture()
    engine.collector.cache.books[buy.id] = capture(
        "synthetic-buy", buy, received_at=NOW - timedelta(seconds=90)
    )
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    body = engine.store.saved[0][1]
    assert body["status"] == "INVALIDATED"
    assert "BOOK_STALE" in body["reasons"]
    assert body["actionable"] is False




























