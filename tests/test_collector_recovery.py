"""SYNTHETIC transport fixtures; these tests make no public API connections."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from arbiq.auth.security import hash_password
from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import Schedule
from arbiq.collector.worker import Collector, ExchangeRuntime
from arbiq.connectors import kraken_ws
from arbiq.connectors.kraken_ws import (
    KrakenBook,
    KrakenStream,
    ResynchronizationRequired,
    SequenceGuard,
)
from arbiq.core.config import Settings
from arbiq.market_data.discovery import normalize_market
from test_market_data import synthetic_capture, synthetic_market, synthetic_raw


def synthetic_ws_snapshot():
    return {
        "symbol": "BTC/USD",
        "bids": [{"price": "99", "qty": "10"}, {"price": "98", "qty": "2"}],
        "asks": [{"price": "101", "qty": "3"}, {"price": "102", "qty": "2"}],
        "checksum": 1882172307,
    }


def test_native_checksum_snapshot_delta_and_resynchronization():
    book = KrakenBook(10)
    with pytest.raises(ResynchronizationRequired, match="DELTA_WITHOUT_SNAPSHOT"):
        book.apply(synthetic_ws_snapshot(), snapshot=False)
    value = book.apply(synthetic_ws_snapshot(), snapshot=True)
    assert value["bids"][0][0].as_tuple().exponent == 0
    assert book.checksum() == 1882172307
    # Zero-quantity removal is a legal delta; a wrong checksum invalidates local state.
    with pytest.raises(ResynchronizationRequired, match="CHECKSUM_MISMATCH"):
        book.apply(
            {
                "symbol": "BTC/USD",
                "bids": [{"price": "99", "qty": "0"}],
                "asks": [],
                "checksum": 1,
            },
            False,
        )
    assert not book.initialized
    assert book.apply(synthetic_ws_snapshot(), snapshot=True)["symbol"] == "BTC/USD"
    sequence = SequenceGuard()
    sequence.accept(100, snapshot=True)
    sequence.accept(101)
    with pytest.raises(ResynchronizationRequired, match="SEQUENCE_GAP"):
        sequence.accept(103)
    sequence.accept(500, snapshot=True)
    sequence.accept(501)


async def test_native_websocket_disconnect_reconnects_with_new_snapshot(monkeypatch):
    stop = asyncio.Event()
    attempts = 0
    received = []
    errors = []

    class Socket:
        def __init__(self, number):
            self.number = number

        async def send(self, value):
            assert json.loads(value)["params"]["snapshot"] is True

        async def recv(self):
            if self.number == 1:
                raise OSError("SYNTHETIC disconnect")
            return json.dumps(
                {
                    "channel": "book",
                    "type": "snapshot",
                    "data": [synthetic_ws_snapshot()],
                }
            )

    class Connection:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            return Socket(attempts)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(kraken_ws, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(kraken_ws, "retry_delay", lambda *args, **kwargs: 0)

    async def on_book(symbol, book):
        received.append((symbol, book))
        stop.set()

    async def on_error(code, reconnects):
        errors.append((code, reconnects))

    stream = KrakenStream(CollectorConfig(), on_book, on_error)
    await asyncio.wait_for(stream.run(["BTC/USD"], stop), 2)
    assert attempts == 2 and len(received) == 1
    assert errors == [("WS_DISCONNECTED", 1)]


def settings(config):
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://synthetic:synthetic@db/quantix_test",
        admin_password_hash=hash_password("synthetic-password-only"),
        collector=config,
    )


async def test_exchange_keeps_polling_the_prior_selection_while_watermarks_load():
    entered = asyncio.Event()
    release = asyncio.Event()
    slug = "synthetic-venue"
    previous, selected = [
        replace(normalize_market(slug, synthetic_market(base), {}, precision_mode=4), id=uuid4())
        for base in ("BTC", "ETH")
    ]
    now = datetime.now(UTC)

    async def latest_watermarks(ids):
        assert ids == {selected.id}
        entered.set()
        await release.wait()
        return {selected.id: (now, now)}

    store = SimpleNamespace(
        latest_watermarks=latest_watermarks,
        register_exchange=AsyncMock(return_value=uuid4()),
        event=AsyncMock(),
        retain_runtime_keys=Mock(),
    )
    collector = Collector(settings(CollectorConfig()), store)
    connector = SimpleNamespace(
        adapter=SimpleNamespace(id=slug, name="SYNTHETIC venue", has={}),
        close=AsyncMock(),
    )
    runtime = ExchangeRuntime(
        slug, connector, markets=[previous], schedules={previous.id: Schedule()}
    )
    runtime.discovery.due = float("inf")
    collector.runtimes[slug] = runtime

    async def fetch(active_runtime, market):
        assert active_runtime.schedules[market.id].due == 0
        assert market.id == previous.id
        collector.stop.set()

    collector.fetch = AsyncMock(side_effect=fetch)
    selection = asyncio.create_task(collector.apply_selection(runtime, [selected]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(collector.run_exchange(slug), 2)
        collector.fetch.assert_awaited_once_with(runtime, previous)
    finally:
        release.set()
        await asyncio.wait_for(selection, 2)
    assert runtime.markets == [selected]
    assert set(runtime.schedules) == {selected.id}
    assert collector.cache.watermarks[selected.id] == (now, now)


@pytest.mark.parametrize("result", ["success", "timeout", "malformed"])
async def test_deselected_rest_fetch_cannot_mutate_a_reselected_market(result):
    entered = asyncio.Event()
    release = asyncio.Event()
    slug = "synthetic-venue"
    market = replace(normalize_market(slug, synthetic_market(), {}, precision_mode=4), id=uuid4())

    async def fetch_raw_book(symbol):
        if result == "malformed":
            raise ValueError("synthetic malformed response")
        entered.set()
        await release.wait()
        if result == "timeout":
            raise TimeoutError("synthetic late timeout")
        return synthetic_raw()

    async def malformed(*_args):
        assert result == "malformed"
        entered.set()
        await release.wait()

    store = SimpleNamespace(
        latest_watermarks=AsyncMock(return_value={}),
        event=AsyncMock(),
        retain_runtime_keys=Mock(),
        save_book=AsyncMock(),
        malformed=malformed,
    )
    collector = Collector(settings(CollectorConfig()), store)
    runtime = ExchangeRuntime(
        slug,
        SimpleNamespace(fetch_raw_book=fetch_raw_book, last_latency_ms=10),
        markets=[market],
        schedules={market.id: Schedule()},
    )
    collector.runtimes[slug] = runtime
    assert collector.request_confirmation(market.id) == "QUEUED"
    task = asyncio.create_task(collector.fetch(runtime, market))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await collector.apply_selection(runtime, [])
        await collector.apply_selection(runtime, [market])
        assert collector.request_confirmation(market.id) == "QUEUED"
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
    assert collector.cache.get(market.id) is None
    assert market.id in runtime.confirmation and market.id in collector.confirmation_enqueued
    assert runtime.schedules[market.id].failures == 0
    assert runtime.health["error_code"] is None
    store.save_book.assert_not_awaited()


async def test_worker_enforces_global_concurrency_and_handles_timeout():
    config = CollectorConfig(max_concurrency=2)
    store = AsyncMock()
    collector = Collector(settings(config), store)
    active = 0
    maximum = 0

    class Connector:
        last_latency_ms = 10

        async def fetch_raw_book(self, symbol):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return synthetic_raw()

    requests = []
    for index in range(6):
        market = normalize_market(f"synthetic-{index}", synthetic_market(), {}, precision_mode=4)
        from dataclasses import replace

        market = replace(market, id=uuid4())
        runtime = ExchangeRuntime(
            f"synthetic-{index}",
            Connector(),
            exchange_id=uuid4(),
            markets=[market],
            schedules={market.id: Schedule()},
        )
        collector.runtimes[runtime.slug] = runtime
        requests.append(collector.fetch(runtime, market))
    await asyncio.gather(*requests)
    assert maximum == 2 and store.save_book.await_count == 6
    runtime = next(iter(collector.runtimes.values()))
    market = runtime.markets[0]
    runtime.connector.fetch_raw_book = AsyncMock(side_effect=TimeoutError("synthetic timeout"))
    await collector.fetch(runtime, market)
    assert runtime.health["error_code"] == "TIMEOUT"
    assert runtime.schedules[market.id].failures == 1
    assert store.event.await_count >= 1


async def test_anomaly_recheck_queue_is_bounded_deduplicated_and_survives_first_fetch():
    store = AsyncMock()
    collector = Collector(settings(CollectorConfig()), store)
    from dataclasses import replace

    market = replace(
        normalize_market("synthetic-c", synthetic_market(), {}, precision_mode=4),
        id=uuid4(),
    )
    raw = synthetic_raw()
    raw["bids"] = [[str(200 - i), "1"] for i in range(10)]
    raw["asks"] = [[str(201 + i), "1"] for i in range(10)]
    connector = type(
        "SyntheticConnector",
        (),
        {"last_latency_ms": 10, "fetch_raw_book": AsyncMock(return_value=raw)},
    )()
    runtime = ExchangeRuntime(
        "synthetic-c",
        connector,
        exchange_id=uuid4(),
        markets=[market],
        schedules={market.id: Schedule()},
    )
    collector.runtimes[runtime.slug] = runtime
    for exchange in ("synthetic-a", "synthetic-b"):
        collector.cache.put(synthetic_capture(exchange=exchange))
    await collector.fetch(runtime, market)
    assert market.id in runtime.confirmation
    assert collector.request_confirmation(market.id) == "COOLDOWN"
    assert collector.confirmation_queue.qsize() == 1
    await collector.fetch(runtime, market)
    assert collector.cache.get(market.id).verification["fresh_rest_recheck"] is True
    assert market.id not in runtime.confirmation
    assert collector.request_confirmation(uuid4()) == "NOT_MONITORED"


async def test_invalid_response_never_marks_a_feed_as_live_verified():
    store = AsyncMock()
    collector = Collector(settings(CollectorConfig()), store)
    from dataclasses import replace

    market = replace(
        normalize_market("synthetic", synthetic_market(), {}, precision_mode=4),
        id=uuid4(),
    )
    runtime = ExchangeRuntime("synthetic", None)
    now = datetime.now(UTC)
    await collector.accept(runtime, market, {}, now, now, 10, "rest")
    assert runtime.health["last_success_at"] is None
    assert runtime.health["error_code"] == "INVALID_BOOK"




