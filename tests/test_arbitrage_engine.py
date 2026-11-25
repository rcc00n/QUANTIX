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


@pytest.mark.asyncio
async def test_exchange_disconnect_is_visible_and_nonactionable():
    engine, buy, sell, _ = engine_fixture()
    engine.market_map[buy.id][0].health["error_code"] = "NETWORK_UNAVAILABLE"
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    body = engine.store.saved[0][1]
    assert "EXCHANGE_CONNECTIVITY_DEGRADED" in body["reasons"]
    assert body["actionable"] is False and body["status"] != "VERIFIED_MARKET"


@pytest.mark.asyncio
async def test_coinbase_adapters_are_one_underlying_venue():
    engine, buy, sell, requests = engine_fixture(buy_slug="coinbase", sell_slug="coinbaseexchange")
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    assert not engine.store.saved and not requests
    assert engine.diagnostics()["metrics"]["compatible_intersections"] == 0


@pytest.mark.asyncio
async def test_extreme_dislocation_preserves_shallow_evidence_and_requests_fresh_rechecks():
    engine, buy, sell, requests = engine_fixture()
    book = capture("synthetic-buy", buy, ask="50", bid="49", quantities=["0.01"] * 3)
    engine.collector.cache.books[buy.id] = book.classified(
        DataState.SUSPICIOUS,
        reasons=("extreme_reference_discrepancy",),
        verification={
            "status": "REPEATED_WITH_REFERENCE_DISAGREEMENT",
            "fresh_rest_recheck": True,
            "independent_reference_count": 3,
        },
    )
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    _, body, retained, _ = engine.store.saved[0]
    assert body["status"] == "RESEARCH_ONLY"
    assert "ANOMALY_REQUIRES_FRESH_INDEPENDENT_RECHECK" in body["reasons"]
    assert requests == {buy.id, sell.id}
    assert retained.raw_evidence == book.raw_evidence
    assert Decimal(body["quantity"]) <= Decimal("0.03")
    assert Decimal(body["net_profit_cad"]) < Decimal(2)


@pytest.mark.asyncio
async def test_crossed_book_cannot_produce_executable_quantity():
    engine, buy, sell, requests = engine_fixture()
    engine.collector.cache.books[buy.id] = capture("synthetic-buy", buy, ask="90", bid="100")
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    assert not engine.store.saved and not requests
    assert engine.invalidations["INVALID_MARKET_METADATA_OR_INSUFFICIENT_DEPTH"] == 1


@pytest.mark.asyncio
async def test_absent_fx_suppresses_cad_projection_without_stablecoin_parity():
    engine, _, _, _ = engine_fixture()
    buy, sell = (
        market("synthetic-buy", quote="USDC"),
        market("synthetic-sell", quote="USDC"),
    )
    for slug, discovered, ask, bid in (
        ("synthetic-buy", buy, "100", "99"),
        ("synthetic-sell", sell, "111", "110"),
    ):
        rt = engine.collector.runtimes[slug]
        rt.markets = rt.catalog = [discovered]
        engine.collector.cache.books[discovered.id] = capture(slug, discovered, ask=ask, bid=bid)
    engine.rebuild()
    await engine.evaluate_pair(buy.id, sell.id, {}, NOW)
    body = engine.store.saved[0][1]
    assert body["net_profit_cad"] is None
    assert body["acquisition_cost_cad"] is None
    assert body["status"] == "RESEARCH_ONLY"
    assert any("conversion" in reason.lower() for reason in body["reasons"])
    assert "BUDGET_NOT_APPLIED_CAD_UNAVAILABLE" in body["reasons"]


@pytest.mark.asyncio
async def test_unaffordable_minimum_does_not_fallback_to_over_budget_maximum():
    engine, buy, sell, _ = engine_fixture(config=EngineConfig(default_budget_cad="100"))
    rt, discovered = engine.market_map[buy.id]
    engine.market_map[buy.id] = (
        rt,
        replace(discovered, metadata={**discovered.metadata, "minimum_quantity": "2"}),
    )
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    body = engine.store.saved[0][1]
    assert body["status"] == "INVALIDATED"
    assert body["quantity"] is None
    assert body["net_profit_cad"] is None
    assert "NO_FEASIBLE_EXECUTABLE_SIZE" in body["reasons"]


def test_updates_coalesce_by_asset_and_pending_capacity_is_bounded():
    engine, buy, _, _ = engine_fixture(config=EngineConfig(max_candidates=10))
    book = engine.collector.cache.books[buy.id]
    for _ in range(100):
        engine.changed(book)
    assert engine.pending == {"asset:BTC"}
    for index in range(20):
        engine.changed(book.model_copy(update={"base_key": f"synthetic:{index}"}))
    assert len(engine.pending) == 10
    assert engine.metrics["coalesced_updates"] > 0


@pytest.mark.asyncio
async def test_unchanged_route_does_not_write_each_tick_and_rate_budget_is_bounded():
    engine, buy, sell, _ = engine_fixture(config=EngineConfig(max_writes_per_minute=1))
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    assert len(engine.store.saved) == 1
    key = next(iter(engine.current))
    engine.last_write[key] = time.monotonic() - engine.config.persistence_interval_seconds - 1
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    assert len(engine.store.saved) == 1
    assert engine.metrics["persistence_rate_limited"] == 1


@pytest.mark.asyncio
async def test_disappeared_spread_invalidates_prior_evaluation():
    engine, buy, sell, _ = engine_fixture()
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    engine.collector.cache.books[sell.id] = capture("synthetic-sell", sell, ask="100", bid="99")
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    assert next(iter(engine.current.values()))["status"] == "INVALIDATED"
    assert engine.store.transition.await_args.args[1:3] == (
        "INVALIDATED",
        "OBSERVED_SPREAD_DISAPPEARED",
    )


@pytest.mark.asyncio
async def test_malformed_metadata_invalidates_prior_route_without_extending_evidence_age():
    engine, buy, sell, _ = engine_fixture()
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    key = next(iter(engine.current))
    last_seen = engine.last_seen[key]
    rt, discovered = engine.market_map[buy.id]
    engine.market_map[buy.id] = (
        rt,
        replace(
            discovered,
            metadata={**discovered.metadata, "minimum_quantity": "SYNTHETIC-BAD"},
        ),
    )
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    assert engine.current[key]["status"] == "INVALIDATED"
    assert engine.last_seen[key] == last_seen
    assert engine.store.transition.await_args.args[2] == (
        "INVALID_MARKET_METADATA_OR_INSUFFICIENT_DEPTH"
    )


@pytest.mark.asyncio
async def test_deferred_terminal_transition_is_retried_within_shared_write_budget():
    engine, buy, sell, _ = engine_fixture(config=EngineConfig(max_writes_per_minute=1))
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    key = next(iter(engine.current))
    await engine.invalidate(key, "EXPIRED", "SYNTHETIC_EXPIRY", NOW)
    assert engine.current[key]["status"] == "EXPIRED"
    assert key in engine.pending_transitions
    assert not engine.store.transition.await_count
    await engine.flush_transitions()
    assert not engine.store.transition.await_count
    engine.writes.clear()  # Synthetic passage of a complete rate-limit window.
    await engine.flush_transitions()
    assert engine.store.transition.await_count == 1
    assert not engine.pending_transitions
    assert len(engine.writes) == 1


@pytest.mark.asyncio
async def test_terminal_capacity_is_reclaimed_without_growing_causal_maps():
    engine, buy, sell, _ = engine_fixture(config=EngineConfig(max_candidates=10))
    # Synthetic terminal entries are older than TTL even on a newly booted runner.
    expired_before = time.monotonic() - engine.config.route_ttl_seconds - 11
    for index in range(10):
        key = f"synthetic-old-{index}"
        engine.current[key] = {"id": str(uuid4()), "status": "EXPIRED"}
        engine.last_seen[key] = expired_before + index
        engine.last_write[key] = expired_before + index
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    assert len(engine.current) == 10
    assert len(engine.last_seen) == 10 and len(engine.last_write) == 10
    assert "synthetic-old-0" not in engine.current


@pytest.mark.asyncio
async def test_run_expires_unsupported_routes_and_reports_latency():
    engine, buy, sell, _ = engine_fixture()
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, NOW)
    key = hashlib.sha256(f"{buy.id}:{sell.id}".encode()).hexdigest()
    engine.last_seen[key] = time.monotonic() - engine.config.route_ttl_seconds - 1
    engine.store.transition.side_effect = lambda *args: engine.collector.stop.set()
    await asyncio.wait_for(engine.run(), 1)
    assert engine.current[key]["status"] == "EXPIRED"
    assert engine.store.expire_previous_runs.await_count == 1
    assert engine.diagnostics()["healthy"]
    assert engine.diagnostics()["metrics"]["processing_latency_ms"] is not None


def test_identity_index_excludes_wrapped_and_inactive_markets():
    engine, _, _, _ = engine_fixture()
    rt = engine.collector.runtimes["synthetic-buy"]
    wrapped = market("synthetic-buy", base="WBTC")
    inactive = replace(market("synthetic-buy", base="ETH"), active=False)
    rt.markets.extend([wrapped, inactive])
    engine.rebuild()
    assert wrapped.id not in {i for ids in engine.index.values() for i in ids}
    assert inactive.id not in {i for ids in engine.index.values() for i in ids}
