"""SYNTHETIC native transport and collector fallback tests without DNS, sockets, or disk."""

import asyncio
import hashlib
import json
from collections import Counter, deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from arbiq.collector import worker
from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import Schedule
from arbiq.collector.stream_selection import native_symbols, tier
from arbiq.collector.worker import Collector, ExchangeRuntime
from arbiq.connectors import native_stream
from arbiq.connectors.coinbase_ws import CoinbaseProtocol
from arbiq.connectors.native_stream import NativeStream
from arbiq.connectors.stream_protocol import StreamFrame, StreamInvalid
from arbiq.core.clock import elapsed_seconds
from arbiq.market_data.discovery import normalize_market
from arbiq.market_data.quality import DataState
from arbiq.opportunities import engine as scanner_module
from arbiq.opportunities.contracts import identity_cad
from test_arbitrage_engine import engine_fixture
from test_market_data import synthetic_market

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTC-USD"
ETH = "ETH-USD"
EXCHANGE = "coinbaseexchange"


class ScriptedSocket:
    def __init__(self, *frames):
        self.frames = deque(frames)
        self.sent = []
        self.drained = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def recv(self, *, decode):
        assert decode is False
        if self.frames:
            value = self.frames.popleft()
            if isinstance(value, Exception):
                raise value
            return value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.drained.set()
        await asyncio.Future()

    async def send(self, message):
        self.sent.append(message)


class ScriptedProtocol:
    ping_message = None

    def __init__(self, frames):
        self.frames = frames

    def consume(self, message):
        return self.frames[message.decode()]


def raw_book(product=SYMBOL, *, stamp=NOW, received=NOW, amount="1"):
    return {
        "symbol": product,
        "bids": [["100", amount]],
        "asks": [["101", "2"]],
        "timestamp": stamp.isoformat() if stamp else None,
        "timestamp_basis": "SYNTHETIC_TEST",
        "stream_integrity": {"fixture": "SYNTHETIC_TEST"},
        "_collector_received_at": received,
    }


async def test_disconnect_and_invalid_delta_reconnect_only_with_fresh_native_snapshot(monkeypatch):
    first = ScriptedSocket(OSError("SYNTHETIC network drop"))
    second = ScriptedSocket(
        {
            "type": "l2update",
            "product_id": SYMBOL,
            "changes": [["buy", "100", "9"]],
            "time": NOW.isoformat(),
        }
    )
    third = ScriptedSocket(
        {"type": "snapshot", "product_id": SYMBOL, "bids": [["100", "1"]], "asks": [["101", "2"]]}
    )
    sockets = iter((first, second, third))
    settings = []
    attempts = []
    publications = []
    stop = asyncio.Event()

    def connect(url, **kwargs):
        settings.append((url, kwargs))
        return next(sockets)

    def immediate(attempt, _settings):
        attempts.append(attempt)
        return 0

    async def on_book(symbol, book):
        publications.append((symbol, book))
        stop.set()

    errors, activity = AsyncMock(), AsyncMock()
    monkeypatch.setattr(native_stream, "retry_delay", immediate)
    stream = NativeStream(
        CollectorConfig(),
        CoinbaseProtocol,
        on_book,
        errors,
        activity,
        lambda: NOW,
        connection=connect,
    )
    await asyncio.wait_for(stream.run([SYMBOL], stop), 2)
    assert attempts == [1, 2]
    assert [call.args for call in errors.await_args_list] == [
        ("WS_DISCONNECTED", 1),
        ("DELTA_WITHOUT_SNAPSHOT", 2),
    ]
    assert all(s.closed and len(s.sent) == 1 for s in (first, second, third))
    assert all(json.loads(s.sent[0])["product_ids"] == [SYMBOL] for s in (first, second, third))
    assert all(kw["max_queue"] == 8 and kw["max_size"] == 2**21 for _, kw in settings)
    assert len(publications) == 1 and publications[0][0] == SYMBOL
    result = publications[0][1]
    assert result["timestamp"] is None and result["_collector_received_at"] == NOW
    assert result["stream_integrity"]["subscription_generation"] == 3
    assert activity.await_args.args[:2] == (frozenset({SYMBOL}), NOW)
    assert stream.counts["invalid_sessions"] == 1
    assert stream.pending == {}


async def test_latest_depth_queue_coalesces_by_symbol_and_does_not_republish_heartbeats():
    synchronized = frozenset({SYMBOL, ETH})
    latest = raw_book(amount="7")
    frames = {
        "a": StreamFrame(((SYMBOL, raw_book()),), synchronized),
        "b": StreamFrame(((ETH, raw_book(ETH)),), synchronized),
        "newer": StreamFrame(((SYMBOL, latest),), synchronized),
        "duplicate": StreamFrame(((SYMBOL, latest),), synchronized),
        "heartbeat": StreamFrame(synchronized=synchronized),
        "unsupported": StreamFrame(synchronized=synchronized, activity=False),
    }
    socket = ScriptedSocket(*frames)
    all_published = asyncio.Event()
    outputs = []

    async def on_book(symbol, raw):
        outputs.append((symbol, raw))
        if len(outputs) == 2:
            all_published.set()

    traffic = AsyncMock()
    stream = NativeStream(CollectorConfig(), Mock(), on_book, AsyncMock(), traffic, lambda: NOW)
    receive = asyncio.create_task(stream.receive(socket, ScriptedProtocol(frames), synchronized))
    publisher = None
    try:
        await asyncio.wait_for(socket.drained.wait(), 2)
        assert set(stream.pending) == set(synchronized)
        assert stream.counts["coalesced_depth"] == 1
        assert stream.counts["unchanged_depth"] == 1
        assert traffic.await_count == 5  # Includes heartbeat and ignores unsupported traffic.
        publisher = asyncio.create_task(stream.publish())
        await asyncio.wait_for(all_published.wait(), 2)
        assert [(name, raw["bids"][0][1]) for name, raw in outputs] == [(SYMBOL, "7"), (ETH, "1")]
        for _, raw in outputs:
            assert raw["_collector_received_at"] == NOW
            assert raw["_stream_queue_ms"] >= 0
        assert stream.counts["published_depth"] == 2
        assert stream.pending == {}
    finally:
        tasks = [task for task in (receive, publisher) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "first_synchronized,activity,endpoint,code",
    [
        (False, True, 2, "WS_SNAPSHOT_TIMEOUT"),
        (True, False, 15, "WS_IDLE_TIMEOUT"),
        (True, False, 31, "WS_PONG_TIMEOUT"),
    ],
)
async def test_transport_deadlines_use_elapsed_clock(
    monkeypatch, first_synchronized, activity, endpoint, code
):
    tick = iter([0, endpoint] if code != "WS_PONG_TIMEOUT" else [0, 20, 31])
    current = 0

    class TimedSocket(ScriptedSocket):
        async def recv(self, *, decode):
            nonlocal current
            data = await super().recv(decode=decode)
            current = next(tick)
            return data

    socket = TimedSocket("first", "after", "after")
    symbols = frozenset({SYMBOL})
    protocol = ScriptedProtocol(
        {
            "first": StreamFrame(synchronized=symbols if first_synchronized else frozenset()),
            "after": StreamFrame(
                synchronized=symbols if first_synchronized else frozenset(), activity=activity
            ),
        }
    )
    if code == "WS_PONG_TIMEOUT":
        protocol.ping_message = "ping"
    monkeypatch.setattr(native_stream, "elapsed_seconds", lambda: current)
    config = CollectorConfig(
        websocket_snapshot_timeout_seconds=2,
        websocket_idle_timeout_seconds=45 if code == "WS_PONG_TIMEOUT" else 15,
    )
    stream = NativeStream(config, Mock(), AsyncMock(), AsyncMock(), AsyncMock(), lambda: NOW)
    with pytest.raises(StreamInvalid, match=code):
        await asyncio.wait_for(stream.receive(socket, protocol, symbols), 2)
    assert socket.sent == (["ping"] if code == "WS_PONG_TIMEOUT" else [])


async def test_future_native_timestamp_is_rejected_before_health_or_depth_publication():
    synchronized = frozenset({SYMBOL})
    protocol = ScriptedProtocol(
        {
            "future": StreamFrame(
                ((SYMBOL, raw_book(stamp=NOW + timedelta(seconds=1))),), synchronized
            )
        }
    )
    traffic = AsyncMock()
    stream = NativeStream(CollectorConfig(), Mock(), AsyncMock(), AsyncMock(), traffic, lambda: NOW)
    with pytest.raises(StreamInvalid, match="WS_INVALID_SOURCE_TIME"):
        await asyncio.wait_for(stream.receive(ScriptedSocket("future"), protocol, synchronized), 2)
    traffic.assert_not_awaited()
    assert stream.pending == {}


def listed(slug, base, quote="USD"):
    return replace(
        normalize_market(slug, synthetic_market(base, quote), {}, precision_mode=4),
        id=uuid4(),
    )


def test_native_selection_preserves_market_set_and_groups_coinbase_aliases():
    priorities = ["BTC", "ETH", "SOL", "XRP"]
    listed_markets = [listed(EXCHANGE, asset) for asset in (*priorities, "LTC")]
    monitored = {
        EXCHANGE: listed_markets,
        "coinbase": [listed("coinbase", asset) for asset in priorities],
    }
    assert native_symbols(EXCHANGE, monitored, priorities, 2) == {}
    monitored["kraken"] = [listed("kraken", asset) for asset in priorities]
    market_ids = tuple(m.id for m in monitored[EXCHANGE])
    selected = native_symbols(EXCHANGE, monitored, priorities, 2)
    assert list(selected) == [SYMBOL, ETH]
    assert tuple(m.id for m in monitored[EXCHANGE]) == market_ids
    fast = {m.id for m in selected.values()}
    assert [tier(m, priorities, fast) for m in listed_markets] == [
        "FAST_LANE",
        "FAST_LANE",
        "STANDARD",
        "STANDARD",
        "RESEARCH",
    ]


class ControlledNativeStream:
    def __init__(self, _config, _protocol, on_book, on_error, on_message, _now, **_kwargs):
        self.on_book, self.on_error, self.on_message = on_book, on_error, on_message
        self.counts = Counter()

    async def run(self, _symbols, stop):
        await stop.wait()


@pytest.fixture
async def collector_fixture(monkeypatch):
    config = CollectorConfig(
        exchange_ids=[EXCHANGE, "kraken"], minimum_levels=1, websocket_fast_markets_per_exchange=1
    )
    store = SimpleNamespace(
        save_book=AsyncMock(return_value=False),
        event=AsyncMock(),
        register_exchange=AsyncMock(return_value=uuid4()),
    )
    collector = Collector(SimpleNamespace(collector=config), store)
    simulation = SimpleNamespace(now=NOW)
    collector.utc_clock = SimpleNamespace(
        utcnow=lambda: simulation.now,
        snapshot=lambda: {"trusted": collector.utc_clock.trusted},
        trusted=True,
    )
    collector.cache.now = collector.utcnow
    btc, eth = [listed(EXCHANGE, asset) for asset in ("BTC", "ETH")]
    connector = SimpleNamespace(
        adapter=SimpleNamespace(id="synthetic-exchange", has={}, name="SYNTHETIC Coinbase adapter"),
        close=AsyncMock(),
        fetch_raw_book=AsyncMock(),
        last_latency_ms=10,
    )
    runtime = ExchangeRuntime(EXCHANGE, connector, markets=[btc, eth])
    runtime.schedules = {market.id: Schedule() for market in runtime.markets}
    runtime.discovery.due = float("inf")
    collector.runtimes[EXCHANGE] = runtime
    collector.runtimes["kraken"] = ExchangeRuntime(
        "kraken", SimpleNamespace(), markets=[listed("kraken", asset) for asset in ("BTC", "ETH")]
    )
    monkeypatch.setattr(worker, "NativeStream", ControlledNativeStream)
    await collector.configure_native(runtime)
    try:
        yield SimpleNamespace(
            collector=collector,
            runtime=runtime,
            store=store,
            simulation=simulation,
            btc=btc,
            eth=eth,
        )
    finally:
        collector.stop.set()
        if runtime.ws_task:
            await asyncio.gather(runtime.ws_task, return_exceptions=True)


async def stream_book(fixture, *, stamp=NOW):
    now = fixture.simulation.now
    stream = fixture.runtime.native_stream
    await stream.on_message(frozenset({SYMBOL}), now, elapsed_seconds())
    await stream.on_book(SYMBOL, raw_book(stamp=stamp, received=now))
    return fixture.collector.cache.books[fixture.btc.id]


async def test_heartbeat_keeps_connection_live_without_overriding_source_age_or_clock_trust(
    collector_fixture,
):
    f = collector_fixture
    snapshot = await stream_book(f, stamp=None)
    assert snapshot.exchange_timestamp is None
    assert not f.collector.native_book_fresh(f.runtime, f.btc.id, f.simulation.now)
    book = await stream_book(f)
    assert book.state is DataState.HEALTHY and book.received_at == NOW
    assert book.symbol == "BTC/USD" and book.source == f"{EXCHANGE}_ws"
    assert f.collector.native_book_fresh(f.runtime, f.btc.id, f.simulation.now)
    f.collector.utc_clock.trusted = False
    assert not f.collector.native_book_fresh(f.runtime, f.btc.id, f.simulation.now)
    f.collector.utc_clock.trusted = True
    f.simulation.now += timedelta(seconds=31)
    await f.runtime.native_stream.on_message(
        frozenset({SYMBOL}), f.simulation.now, elapsed_seconds()
    )
    assert f.runtime.ws_synchronized and f.runtime.ws_last_message_at == f.simulation.now
    assert f.collector.cache.books[f.btc.id] is book
    assert not f.collector.native_book_fresh(f.runtime, f.btc.id, f.simulation.now)


async def test_retirement_revokes_stream_preserves_rest_backoff_and_requires_new_book(
    collector_fixture, monkeypatch
):
    f = collector_fixture
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    stream = f.runtime.native_stream
    published = await stream_book(f)
    epoch = published.raw_evidence["stream_integrity"]["collector_integrity_epoch"]
    eth_rest = raw_book("ETH/USD")
    await f.collector.accept(f.runtime, f.eth, eth_rest, NOW, NOW, 10, "rest")
    rest = f.collector.cache.books[f.eth.id]
    f.runtime.schedules[f.btc.id].due = 1100
    f.runtime.schedules[f.eth.id] = Schedule(due=1200, failures=1)
    f.runtime.cooldown_until = 1005
    f.collector.scanner = SimpleNamespace(changed=Mock())

    await stream.on_error("SYNTHETIC_INTEGRITY_FAILURE", 1)
    invalidated = f.collector.cache.books[f.btc.id]
    assert invalidated.state is DataState.STALE
    assert "stream_unsynchronized" in invalidated.reasons
    f.collector.scanner.changed.assert_called_once_with(invalidated)
    assert f.collector.cache.books[f.eth.id] is rest
    assert f.runtime.schedules[f.btc.id].due == 1005
    assert f.runtime.schedules[f.eth.id] == Schedule(due=1200, failures=1)
    assert f.runtime.health["connection_mode"] == "REST fallback"
    assert not f.runtime.native_synchronized and not f.runtime.ws_last

    f.runtime.cooldown_until = 0
    f.runtime.connector.fetch_raw_book.return_value = raw_book("BTC/USD", stamp=None, amount="8")
    f.simulation.now += timedelta(milliseconds=500)
    await f.collector.fetch(f.runtime, f.btc)
    assert f.collector.cache.books[f.btc.id].source == "rest"
    assert not f.runtime.native_synchronized

    f.simulation.now += timedelta(seconds=1)
    await stream.on_message(frozenset({SYMBOL}), f.simulation.now, elapsed_seconds())
    assert not f.collector.native_book_fresh(f.runtime, f.btc.id, f.simulation.now)
    # The old cache stays revoked through resubscription until there is new priced evidence.
    recovered = await stream_book(f, stamp=f.simulation.now)
    assert f.collector.native_book_fresh(f.runtime, f.btc.id, f.simulation.now)
    assert recovered.raw_evidence["stream_integrity"]["collector_integrity_epoch"] > epoch
    f.runtime.schedules[f.btc.id] = Schedule(due=1200, failures=1)
    await stream.on_error("SYNTHETIC_SECOND_FAILURE", 2)
    assert f.runtime.schedules[f.btc.id] == Schedule(due=1200, failures=1)








