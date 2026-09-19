"""Native Kraken v2 L2 snapshots/deltas with exact-decimal CRC32 validation."""

import asyncio
import json
import zlib
from decimal import Decimal

from websockets.asyncio.client import connect

from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import retry_delay
from arbiq.core.clock import elapsed_seconds
from arbiq.finance.contracts import exact_decimal


class ResynchronizationRequired(ValueError):
    pass


class SequenceGuard:
    """For feeds that advertise contiguous sequence numbers; never apply to REST nonces."""

    def __init__(self):
        self.last: int | None = None

    def accept(self, sequence: int, snapshot: bool = False):
        if not snapshot and (self.last is None or sequence != self.last + 1):
            raise ResynchronizationRequired("SEQUENCE_GAP")
        self.last = sequence


class KrakenBook:
    def __init__(self, depth: int):
        self.depth = depth
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.initialized = False

    @staticmethod
    def _digits(value: Decimal) -> str:
        return format(value, "f").replace(".", "").lstrip("0")

    def checksum(self) -> int:
        content = ""
        for side, reverse in ((self.asks, False), (self.bids, True)):
            for price, quantity in sorted(side.items(), reverse=reverse)[:10]:
                content += self._digits(price) + self._digits(quantity)
        return zlib.crc32(content.encode()) & 0xFFFFFFFF

    def apply(self, data: dict, snapshot: bool) -> dict:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
            self.initialized = True
        elif not self.initialized:
            raise ResynchronizationRequired("DELTA_WITHOUT_SNAPSHOT")
        try:
            for key in ("bids", "asks"):
                side = getattr(self, key)
                for level in data.get(key, []):
                    price, quantity = exact_decimal(level["price"]), exact_decimal(level["qty"])
                    if price <= 0 or quantity < 0:
                        raise ValueError("Invalid WS level")
                    if quantity == 0:
                        side.pop(price, None)
                    else:
                        side[price] = quantity
                limited = dict(sorted(side.items(), reverse=key == "bids")[: self.depth])
                setattr(self, key, limited)
            expected = data.get("checksum")
            if expected is None or self.checksum() != int(expected):
                raise ResynchronizationRequired("CHECKSUM_MISMATCH")
        except (ValueError, KeyError, TypeError):
            self.initialized = False
            raise
        return {
            "symbol": data["symbol"],
            "timestamp": data.get("timestamp"),
            "bids": [[p, q] for p, q in sorted(self.bids.items(), reverse=True)],
            "asks": [[p, q] for p, q in sorted(self.asks.items())],
            "nonce": None,
        }


class KrakenStream:
    def __init__(self, config: CollectorConfig, on_book, on_error, on_message=None, utcnow=None):
        self.config = config
        self.on_book = on_book
        self.on_error = on_error
        self.reconnects = 0
        self.on_message = on_message
        self.utcnow = utcnow

    async def run(self, symbols: list[str], stop: asyncio.Event):
        attempt = 0
        while not stop.is_set():
            try:
                # Kraken supports discrete subscription depths. Trim only after CRC validation.
                depth = next(
                    value for value in (10, 25, 100, 500, 1000) if value >= self.config.depth
                )
                states = {symbol: KrakenBook(depth) for symbol in symbols}
                async with connect(
                    "wss://ws.kraken.com/v2",
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**21,
                    max_queue=16,
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "method": "subscribe",
                                "params": {
                                    "channel": "book",
                                    "symbol": symbols,
                                    "depth": depth,
                                    "snapshot": True,
                                },
                            }
                        )
                    )
                    while not stop.is_set():
                        message = await asyncio.wait_for(
                            ws.recv(), self.config.websocket_idle_timeout_seconds
                        )
                        received = self.utcnow() if self.utcnow else None
                        parse_started = elapsed_seconds()
                        payload = json.loads(message, parse_float=Decimal)
                        if payload.get("success") is False:
                            raise ResynchronizationRequired("SUBSCRIPTION_REJECTED")
                        if self.on_message and payload.get("channel") in {"book", "heartbeat"}:
                            await self.on_message(
                                all(state.initialized for state in states.values())
                            )
                        if payload.get("channel") != "book":
                            continue
                        for data in payload.get("data", []):
                            symbol = data.get("symbol")
                            if symbol not in states:
                                raise ResynchronizationRequired("UNKNOWN_STREAM_SYMBOL")
                            raw = states[symbol].apply(data, payload.get("type") == "snapshot")
                            raw["timestamp_basis"] = "exchange_book_event"
                            raw["_collector_received_at"] = received
                            raw["_stream_parse_ms"] = (elapsed_seconds() - parse_started) * 1000
                            await self.on_book(symbol, raw)
                            attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                self.reconnects += 1
                code = str(exc) if isinstance(exc, ResynchronizationRequired) else "WS_DISCONNECTED"
                await self.on_error(code, self.reconnects)
                # Reconnect means new subscriptions + fresh snapshots; no stale delta replay.
                try:
                    await asyncio.wait_for(stop.wait(), retry_delay(attempt, self.config))
                except TimeoutError:
                    pass
