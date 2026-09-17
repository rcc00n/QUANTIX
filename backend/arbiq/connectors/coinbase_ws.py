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


