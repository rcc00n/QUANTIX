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


class BookCache:
    def __init__(self, config: CollectorConfig):
        self.config = config
        self.now = lambda: datetime.now(UTC)
        self.limit = len(config.exchange_ids) * config.max_markets_per_exchange
        self.books: OrderedDict[UUID, CapturedBook] = OrderedDict()
        self.rejected_updates = Counter()
        self.watermarks = OrderedDict()

    def seed_watermarks(self, values):
        for market_id, timestamps in values.items():
            if market_id not in self.books:
                self.watermarks[market_id] = timestamps
        while len(self.watermarks) > self.limit:
            self.watermarks.popitem(last=False)

    def rejection_reason(self, book):
        previous = self.books.get(book.market_id)
        if previous is None:
            watermark = self.watermarks.get(book.market_id)
            if watermark:
                received, source = watermark
                if book.received_at < received or (
                    book.exchange_timestamp and source and book.exchange_timestamp < source
                ):
                    return "BEFORE_RESTART_WATERMARK"
            return None
        previous_time_trusted = temporal_order_trusted(previous)
        if previous_time_trusted and book.received_at < previous.received_at:
            return "OUT_OF_ORDER_RECEIPT"
        if (
            book.ingestion_index
            and previous.ingestion_index
            and book.ingestion_index < previous.ingestion_index
        ):
            return "OUT_OF_ORDER_INGESTION"
        if (
            previous_time_trusted
            and book.exchange_timestamp
            and previous.exchange_timestamp
            and book.exchange_timestamp < previous.exchange_timestamp
        ):
            return "OUT_OF_ORDER_SOURCE"
        if (
            previous_time_trusted
            and book.source.endswith("_ws")
            and book.content_sha256 == previous.content_sha256
        ):
            return "DUPLICATE_STREAM_UPDATE"
        return None

