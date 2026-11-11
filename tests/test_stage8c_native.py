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








def listed(slug, base, quote="USD"):
    return replace(
        normalize_market(slug, synthetic_market(base, quote), {}, precision_mode=4),
        id=uuid4(),
    )




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












