"""OKX public spot L2 books with linked sequence validation."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from arbiq.connectors.stream_protocol import StreamFrame, StreamInvalid
from arbiq.finance.contracts import exact_decimal

_CHANNEL = "books"
_CHANNEL_DEPTH = 400
_SPOT_INSTRUMENT = re.compile(r"[A-Z0-9]+-[A-Z0-9]+", re.ASCII)

Levels = list[tuple[Decimal, Decimal]]


@dataclass(slots=True)
class _Book:
    bids: dict[Decimal, Decimal]
    asks: dict[Decimal, Decimal]
    sequence: int
    timestamp: int
    last_packet: dict


def _sequence(value: object, minimum: int) -> int:
    if type(value) is not int or not minimum <= value < 2**63:
        raise StreamInvalid("INVALID_SEQUENCE")
    return value


def _source_timestamp(value: object) -> int:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 15
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise StreamInvalid("INVALID_BOOK_TIMESTAMP")
    return int(value)


def _levels(rows: object, *, snapshot: bool) -> Levels:
    max_rows = _CHANNEL_DEPTH if snapshot else 2 * _CHANNEL_DEPTH
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise StreamInvalid("INVALID_BOOK_LEVELS")
    levels: Levels = []
    prices: set[Decimal] = set()
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != 4
            or not isinstance(row[0], str)
            or not isinstance(row[1], str)
        ):
            raise StreamInvalid("INVALID_BOOK_LEVEL")
        try:
            price, size = exact_decimal(row[0]), exact_decimal(row[1])
        except (ValueError, ArithmeticError) as exc:
            raise StreamInvalid("INVALID_BOOK_LEVEL") from exc
        if price <= 0 or size < 0 or (snapshot and size == 0) or price in prices:
            raise StreamInvalid("INVALID_BOOK_LEVEL")
        prices.add(price)
        levels.append((price, size))
    return levels


def _apply_levels(
    existing: dict[Decimal, Decimal], levels: Levels, *, reverse: bool, depth: int
) -> list[list[Decimal]]:
    for price, size in levels:
        if size:
            existing[price] = size
        else:
            existing.pop(price, None)
    sorted_prices = sorted(existing, reverse=reverse)
    for price in sorted_prices[_CHANNEL_DEPTH:]:
        del existing[price]
    return [[price, existing[price]] for price in sorted_prices[:depth]]


class OkxProtocol:
    url = "wss://ws.okx.com/ws/v5/public"
    ping_message = "ping"

    def __init__(self, symbols: list[str], depth: int):
        if (
            not 1 <= len(symbols) < 30
            or len(symbols) != len(set(symbols))
            or any(not _SPOT_INSTRUMENT.fullmatch(symbol) for symbol in symbols)
        ):
            raise ValueError("OKX requires 1–29 unique native spot instrument IDs")
        if not 1 <= depth <= _CHANNEL_DEPTH:
            raise ValueError("OKX books publication depth must be between 1 and 400")
        self._symbols = tuple(symbols)
        self._depth = depth
        self._books: dict[str, _Book] = {}

    def subscription(self) -> dict:
        return {
            "op": "subscribe",
            "args": [{"channel": _CHANNEL, "instId": symbol} for symbol in self._symbols],
        }

    def consume(self, message: str | bytes) -> StreamFrame:
        try:
            return self._consume(message)
        except (UnicodeError, RecursionError, ValueError) as exc:
            self._books.clear()
            if isinstance(exc, StreamInvalid):
                raise
            raise StreamInvalid("INVALID_STREAM_MESSAGE") from exc

    def _consume(self, message: str | bytes) -> StreamFrame:
        if message in ("pong", b"pong"):
            return StreamFrame(synchronized=frozenset(self._books), pong=True)
        payload = json.loads(message)
        if not isinstance(payload, dict):
            raise StreamInvalid("INVALID_STREAM_MESSAGE")
        event = payload.get("event")
        if event == "error":
            raise StreamInvalid("SUBSCRIPTION_REJECTED")
        if event == "notice" and str(payload.get("code")) == "64008":
            raise StreamInvalid("EXCHANGE_MAINTENANCE")
        if event == "notice":
            return StreamFrame(synchronized=frozenset(self._books), activity=False)

        arg = payload.get("arg")
        symbol = arg.get("instId") if isinstance(arg, dict) else None
        if (
            not isinstance(symbol, str)
            or symbol not in self._symbols
            or arg.get("channel") != _CHANNEL
        ):
            raise StreamInvalid("UNKNOWN_STREAM_SYMBOL")
        if event == "subscribe":
            return StreamFrame(synchronized=frozenset(self._books), activity=False)
        action = payload.get("action")
        rows = payload.get("data")
        if (
            event is not None
            or action not in ("snapshot", "update")
            or not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
        ):
            raise StreamInvalid("INVALID_BOOK_MESSAGE")
        raw = self._apply(symbol, rows[0], snapshot=action == "snapshot")
        return StreamFrame(
            books=((symbol, raw),) if raw is not None else (),
            synchronized=frozenset(self._books),
        )

    def _apply(self, symbol: str, packet: dict, *, snapshot: bool) -> dict | None:
        # The current public contract removed CRC values. Quarantine a feed
        # that advertises a live CRC instead of accepting it without validation.
        if "checksum" in packet and (
            type(packet["checksum"]) is not int or packet["checksum"] != 0
        ):
            raise StreamInvalid("UNEXPECTED_CHECKSUM")
        previous = _sequence(packet.get("prevSeqId"), -1)
        sequence = _sequence(packet.get("seqId"), 0)
        timestamp = _source_timestamp(packet.get("ts"))
        book = self._books.get(symbol)
        if snapshot:
            if previous != -1:
                raise StreamInvalid("INVALID_SNAPSHOT_SEQUENCE")
            if book is not None:
                if packet == book.last_packet:
                    return None
                raise StreamInvalid("UNEXPECTED_SNAPSHOT")
        else:
            if book is None:
                raise StreamInvalid("DELTA_WITHOUT_SNAPSHOT")
            if packet == book.last_packet and previous != -1:
                return None
            if previous != book.sequence:
                raise StreamInvalid("SEQUENCE_GAP")
            if sequence < previous:
                # OKX documents maintenance sequence resets. Start with a new full book.
                raise StreamInvalid("SEQUENCE_RESET")
            if sequence == previous:
                if packet.get("bids") == [] and packet.get("asks") == []:
                    return None  # A linked heartbeat carries no quote evidence.
                raise StreamInvalid("CHANGED_DUPLICATE_SEQUENCE")
            if timestamp < book.timestamp:
                raise StreamInvalid("REGRESSED_BOOK_TIMESTAMP")

        bid_changes = _levels(packet.get("bids"), snapshot=snapshot)
        ask_changes = _levels(packet.get("asks"), snapshot=snapshot)
        bid_map = book.bids if book else {}
        ask_map = book.asks if book else {}
        bids = _apply_levels(bid_map, bid_changes, reverse=True, depth=self._depth)
        asks = _apply_levels(ask_map, ask_changes, reverse=False, depth=self._depth)
        self._books[symbol] = _Book(bid_map, ask_map, sequence, timestamp, packet)
        if not snapshot and not bid_changes and not ask_changes:
            return None
        return {
            "symbol": symbol,
            "timestamp": timestamp,
            "timestamp_basis": "exchange_book_event",
            "nonce": None,
            "bids": bids,
            "asks": asks,
            "stream_integrity": {
                "channel": _CHANNEL,
                "instrument": symbol,
                "method": "linked_sequence",
                "verified": True,
                "snapshot": snapshot,
                "sequence": sequence,
                "previous_sequence": previous,
                "channel_depth": _CHANNEL_DEPTH,
            },
        }
