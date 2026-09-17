"""Public Coinbase Exchange Level2 Batch, with bounded exact top-of-book state."""

import json
from datetime import datetime
from decimal import Decimal
from heapq import nlargest, nsmallest
from operator import itemgetter

from arbiq.connectors.stream_protocol import StreamFrame, StreamInvalid
from arbiq.finance.contracts import exact_decimal, utc_timestamp

_BOOK_CHANNELS = {"level2_batch", "level2_50"}
_MAX_SNAPSHOT_SIDE = 100_000
_MAX_BATCH_CHANGES = 8_192
_PRICE_ONLY = itemgetter(0)
_INTEGRITY = {
    "channel": "level2_batch",
    "snapshot": "native_full_depth",
    "size_semantics": "absolute",
    "update_sequence": "not_supplied",
    "checksum": "not_supplied",
}


def _level(price: str, quantity: str) -> tuple[Decimal, Decimal]:
    # Coinbase encodes both monetary fields as decimal strings.
    if not isinstance(price, str) or not isinstance(quantity, str):
        raise StreamInvalid("INVALID_LEVEL_TYPE")
    amount, size = exact_decimal(price), exact_decimal(quantity)
    if amount <= 0 or size < 0:
        raise StreamInvalid("INVALID_LEVEL_VALUE")
    return amount, size


def _event_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise StreamInvalid("INVALID_EVENT_TIMESTAMP")
    try:
        return utc_timestamp(datetime.fromisoformat(value))
    except ValueError as exc:
        raise StreamInvalid("INVALID_EVENT_TIMESTAMP") from exc


class _Side:
    """Retain the known price region, or the entire book for a short snapshot.

    A dropped price is always strictly worse than the retained boundary. Any
    update on the tracked side of that boundary is applied. If the known
    region ceases to contain the requested number of levels, resnapshot instead
    of guessing or treating the unseen tail as absent.
    """

    def __init__(self, rows: list, *, is_bid: bool, capacity: int):
        if not isinstance(rows, list) or not 0 < len(rows) <= _MAX_SNAPSHOT_SIDE:
            raise StreamInvalid("INVALID_SNAPSHOT_DEPTH")
        self.is_bid = is_bid
        self.capacity = capacity
        self.boundary: Decimal | None = None
        self.levels: dict[Decimal, Decimal] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise StreamInvalid("INVALID_SNAPSHOT_LEVEL")
            price, size = _level(*row)
            if size == 0 or price in self.levels:
                raise StreamInvalid("INVALID_SNAPSHOT_LEVEL")
            self.levels[price] = size
        self.trim()

    def apply(self, price: Decimal, size: Decimal):
        if self.boundary is not None and (
            price < self.boundary if self.is_bid else price > self.boundary
        ):
            return
        if size == 0:
            self.levels.pop(price, None)
        else:
            self.levels[price] = size

    def trim(self):
        if len(self.levels) > self.capacity:
            leading = nlargest if self.is_bid else nsmallest
            rows = leading(self.capacity, self.levels.items(), key=_PRICE_ONLY)
            self.levels = dict(rows)
            self.boundary = rows[-1][0]

    def leading(self, depth: int) -> list:
        self.trim()
        if self.boundary is not None and len(self.levels) < depth:
            raise StreamInvalid("DEPTH_FRONTIER_EXHAUSTED")
        prices = sorted(self.levels, reverse=self.is_bid)[:depth]
        return [[price, self.levels[price]] for price in prices]


class _Book:
    def __init__(self, data: dict, depth: int):
        self.depth = depth
        capacity = max(256, depth * 4)
        self.bids = _Side(data["bids"], is_bid=True, capacity=capacity)
        self.asks = _Side(data["asks"], is_bid=False, capacity=capacity)
        self.last_time: datetime | None = None
        self.last_changes: tuple | None = None

    def update(self, data: dict) -> bool:
        timestamp = _event_time(data["time"])
        if self.last_time is not None and timestamp < self.last_time:
            raise StreamInvalid("OUT_OF_ORDER_BOOK_EVENT")
        changes = data["changes"]
        if not isinstance(changes, list) or not 0 < len(changes) <= _MAX_BATCH_CHANGES:
            raise StreamInvalid("INVALID_BATCH_SIZE")
        parsed = []
        for change in changes:
            if not isinstance(change, list) or len(change) != 3:
                raise StreamInvalid("INVALID_BATCH_CHANGE")
            side, price, quantity = change
            if side not in ("buy", "sell"):
                raise StreamInvalid("INVALID_BATCH_SIDE")
            parsed.append((side, *_level(price, quantity)))
        batch = tuple(parsed)
        if timestamp == self.last_time and batch == self.last_changes:
            return False
        for side, price, size in batch:
            (self.bids if side == "buy" else self.asks).apply(price, size)
        self.last_time, self.last_changes = timestamp, batch
        return True

    def projected(self, product: str, event_time: str | None) -> dict:
        bids, asks = self.bids.leading(self.depth), self.asks.leading(self.depth)
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            raise StreamInvalid("INVALID_RECONSTRUCTED_BOOK")
        return {
            "symbol": product,
            "bids": bids,
            "asks": asks,
            "timestamp": event_time,
            "timestamp_basis": (
                "exchange_batch_latest_event" if event_time else "no_verified_snapshot_event_time"
            ),
            "stream_integrity": dict(_INTEGRITY),
            "nonce": None,
        }


