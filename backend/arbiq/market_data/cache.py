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

    def references(self, book: CapturedBook, now: datetime) -> list[dict]:
        by_venue = {}
        for candidate in self.books.values():
            if venue_group(candidate.exchange) == venue_group(book.exchange):
                continue
            if (candidate.base_key, candidate.quote_key) != (book.base_key, book.quote_key):
                continue  # Never treat USD, CAD, USDC or USDT as equivalent.
            candidate = fresh_view(candidate, now, self.config.maximum_age_seconds)
            price = midpoint(candidate)
            if candidate.state == DataState.HEALTHY and price is not None:
                by_venue[venue_group(candidate.exchange)] = {
                    "exchange": candidate.exchange,
                    "market_id": str(candidate.market_id),
                    "price": str(price),
                    "quote_key": candidate.quote_key,
                    "received_at": candidate.received_at.isoformat(),
                }
        return list(by_venue.values())

    def put(self, book: CapturedBook, confirmation: bool = False) -> CapturedBook | None:
        previous = self.books.get(book.market_id)
        rejection = self.rejection_reason(book)
        if rejection:
            self.rejected_updates[rejection] += 1
            return previous
        references = self.references(book, book.received_at)
        price = midpoint(book)
        reasons = list(book.reasons)
        if price and previous and previous.state == DataState.HEALTHY:
            before = midpoint(previous)
            if before and deviation_pct(price, before) > self.config.anomaly_deviation_pct:
                reasons.append("extreme_temporal_discrepancy")
        if price and len(references) >= 2:
            values = sorted(Decimal(item["price"]) for item in references)
            median = values[len(values) // 2]
            if deviation_pct(price, median) > self.config.anomaly_deviation_pct:
                reasons.append("extreme_reference_discrepancy")
        unresolved = bool(
            previous
            and (
                "unconfirmed_anomaly" in previous.reasons
                or any(r.startswith("extreme_") for r in previous.reasons)
            )
        )
        resolved = (
            confirmation
            and len(references) >= 2
            and not any(r.startswith("extreme_") for r in reasons)
            and book.state == DataState.HEALTHY
        )
        if unresolved and not resolved:
            reasons.append("unconfirmed_anomaly")
        anomaly = (
            any(reason.startswith("extreme_") for reason in reasons)
            or "unconfirmed_anomaly" in reasons
        )
        verification = {
            **book.verification,
            "status": "NOT_NEEDED",
            "independent_references": references,
            "same_quote_only": True,
        }
        if anomaly or confirmation:
            repeated = bool(
                price
                and previous
                and midpoint(previous)
                and deviation_pct(price, midpoint(previous)) <= Decimal("2")
            )
            disagreement = "extreme_reference_discrepancy" in reasons
            verification.update(
                {
                    "status": "INSUFFICIENT_REFERENCE"
                    if len(references) < 2
                    else "REPEATED_WITH_REFERENCE_DISAGREEMENT"
                    if repeated and disagreement
                    else "DISPUTED"
                    if disagreement
                    else "CONFIRMED"
                    if confirmation
                    else "PENDING",
                    "fresh_rest_recheck": confirmation,
                    "repeated_price": repeated,
                    "previous_content_sha256": previous.content_sha256 if previous else None,
                }
            )
        if unresolved and resolved:
            verification["resolved_reference_anomaly"] = True
        state = (
            DataState.SUSPICIOUS if anomaly and book.state != DataState.UNAVAILABLE else book.state
        )
        book = book.classified(
            state, reasons=tuple(dict.fromkeys(reasons)), verification=verification
        )
        self.books[book.market_id] = book
        self.watermarks.pop(book.market_id, None)
        self.books.move_to_end(book.market_id)
        while len(self.books) > self.limit:
            self.books.popitem(last=False)
        return book

    def get(self, market_id: UUID, now: datetime | None = None) -> CapturedBook | None:
        value = self.books.get(market_id)
        return (
            fresh_view(value, now or self.now(), self.config.maximum_age_seconds) if value else None
        )

    def retain(self, market_ids: set[UUID]):
        for market_id in list(self.watermarks):
            if market_id not in market_ids:
                del self.watermarks[market_id]
        for market_id in list(self.books):
            if market_id not in market_ids:
                del self.books[market_id]
