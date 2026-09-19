"""Bounded native subscriptions and latest-depth publication."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable

from websockets.asyncio.client import connect

from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import retry_delay
from arbiq.connectors.stream_protocol import DepthProtocol, StreamInvalid
from arbiq.core.clock import elapsed_seconds
from arbiq.market_data.quality import exchange_timestamp


class NativeStream:
    def __init__(
        self,
        config: CollectorConfig,
        protocol: Callable[[list[str], int], DepthProtocol],
        on_book,
        on_error,
        on_message,
        utcnow,
        *,
        clock_trusted=lambda: True,
        connection=connect,
    ):
        self.config = config
        self.protocol = protocol
        self.on_book, self.on_error, self.on_message = on_book, on_error, on_message
        self.utcnow, self.connection = utcnow, connection
        self.clock_trusted = clock_trusted
        self.counts: Counter = Counter()
        self.pending: dict[str, dict] = {}
        self.wake = asyncio.Event()
        self.generation = 0

    async def receive(self, ws, protocol, symbols):
        snapshot_due = elapsed_seconds() + self.config.websocket_snapshot_timeout_seconds
        activity_due = elapsed_seconds() + self.config.websocket_idle_timeout_seconds
        ping_due = elapsed_seconds() + 20 if protocol.ping_message else float("inf")
        pong_due = float("inf")
        synchronized = frozenset()
        last_depth = {}
        while True:
            now = elapsed_seconds()
            if now >= pong_due:
                raise StreamInvalid("WS_PONG_TIMEOUT")
            if synchronized != symbols and now >= snapshot_due:
                raise StreamInvalid("WS_SNAPSHOT_TIMEOUT")
            if now >= activity_due:
                raise StreamInvalid("WS_IDLE_TIMEOUT")
            if now >= ping_due:
                await ws.send(protocol.ping_message)
                ping_due, pong_due = now + 20, now + 10
            deadline = min(activity_due, ping_due, pong_due)
            if synchronized != symbols:
                deadline = min(deadline, snapshot_due)
            try:
                message = await asyncio.wait_for(ws.recv(decode=False), max(0.001, deadline - now))
            except TimeoutError:
                continue
            received_elapsed, received = elapsed_seconds(), self.utcnow()
            self.counts["frames"] += 1
            self.counts["frame_bytes"] += len(message)
            frame = protocol.consume(message)
            parse_ms = (elapsed_seconds() - received_elapsed) * 1000
            if not frame.activity and not frame.books and not frame.pong:
                continue
            if frame.pong:
                pong_due = float("inf")
            if not synchronized <= frame.synchronized <= symbols:
                raise StreamInvalid("WS_SYNCHRONIZATION_LOST")
            synchronized = frame.synchronized
            for symbol, raw in frame.books:
                if symbol not in synchronized:
                    raise StreamInvalid("WS_UNKNOWN_BOOK")
                try:
                    stamp = exchange_timestamp(raw.get("timestamp"))
                except (ValueError, TypeError, OverflowError, OSError) as exc:
                    raise StreamInvalid("WS_INVALID_SOURCE_TIME") from exc
                if stamp and (stamp.year < 2009 or self.clock_trusted() and stamp > received):
                    raise StreamInvalid("WS_INVALID_SOURCE_TIME")
            if frame.activity:
                activity_due = received_elapsed + self.config.websocket_idle_timeout_seconds
                await self.on_message(synchronized, received, received_elapsed)
            for symbol, raw in frame.books:
                self.counts["book_frames"] += 1
                depth = raw["bids"], raw["asks"]
                if last_depth.get(symbol) == depth:
                    self.counts["unchanged_depth"] += 1
                    continue
                last_depth[symbol] = depth
                if symbol in self.pending:
                    self.counts["coalesced_depth"] += 1
                self.pending[symbol] = {
                    **raw,
                    "_collector_received_at": received,
                    "_stream_received_elapsed": received_elapsed,
                    "_stream_parse_ms": parse_ms,
                    "stream_integrity": {
                        **raw.get("stream_integrity", {}),
                        "subscription_generation": self.generation,
                    },
                }
                self.wake.set()
            if self.counts["frames"] % 8 == 0:
                await asyncio.sleep(0)

    async def publish(self):
        due = {}
        while True:
            self.wake.clear()
            now = elapsed_seconds()
            symbol = next((key for key in self.pending if due.get(key, 0) <= now), None)
            if symbol is not None:
                raw = self.pending.pop(symbol)
                due[symbol] = now + self.config.websocket_publish_interval_seconds
                raw["_stream_queue_ms"] = (now - raw["_stream_received_elapsed"]) * 1000
                self.counts["published_depth"] += 1
                await self.on_book(symbol, raw)
                continue
            delay = min((due[key] - now for key in self.pending), default=None)
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except TimeoutError:
                pass

