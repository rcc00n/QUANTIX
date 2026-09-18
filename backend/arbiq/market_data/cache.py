from collections import Counter, OrderedDict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.quality import (
    CapturedBook,
    DataState,
    deviation_pct,
    fresh_view,
    midpoint,
)


def venue_group(exchange: str) -> str:
    return "coinbase" if exchange == "coinbaseexchange" else exchange


def temporal_order_trusted(book: CapturedBook) -> bool:
    return (
        book.verification.get("clock", {}).get("trusted") is not False
        and not {
            "collector_clock_jump",
            "ingestion_clock_reversed",
            "invalid_or_future_timestamp",
        }.intersection(book.reasons)
        and (book.exchange_timestamp is None or book.exchange_timestamp <= book.received_at)
    )


