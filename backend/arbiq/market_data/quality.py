"""Deterministic book validation. Preserve anomalies; never infer opportunities."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from arbiq.collector.config import CollectorConfig
from arbiq.finance.contracts import Contract, UTCTimestamp, exact_decimal
from arbiq.market_data.contracts import BookLevel, NormalizedOrderBook


class DataState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    SUSPICIOUS = "SUSPICIOUS"
    UNAVAILABLE = "UNAVAILABLE"


class CapturedBook(Contract):
    market_id: UUID
    ingestion_index: int = Field(default=0, ge=0)
    exchange: str
    symbol: str
    base_key: str
    quote_key: str
    normalized: NormalizedOrderBook
    exchange_timestamp: UTCTimestamp | None
    received_at: UTCTimestamp
    request_started_at: UTCTimestamp
    latency_ms: int | None = Field(ge=0)
    state: DataState
    reasons: tuple[str, ...]
    source: str
    sequence: str | None
    content_sha256: str
    raw_evidence: dict
    verification: dict = {}
    actionable: Literal[False] = False

    @property
    def pipeline(self):
        return self.verification.get("pipeline", {})

    def with_pipeline(self, **updates):
        # Nested in the existing extensible evidence envelope so old strict readers
        # can consume new retained observations during a QUANTIX-only rollback.
        return self.model_copy(
            update={
                "verification": {
                    **self.verification,
                    "pipeline": {**self.pipeline, **updates},
                }
            }
        )

    def classified(self, state: DataState, **updates) -> "CapturedBook":
        quality = {
            DataState.HEALTHY: "valid",
            DataState.DEGRADED: "incomplete",
            DataState.STALE: "stale",
            DataState.SUSPICIOUS: "invalid",
            DataState.UNAVAILABLE: "invalid",
        }[state]
        return self.model_copy(
            update={
                "state": state,
                "normalized": self.normalized.model_copy(update={"quality": quality}),
                **updates,
            }
        )


def safe_json(value):
    """Public evidence only. Decimal and unexpected scalar values remain strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(v) for v in value]
    if isinstance(value, float):
        return str(value)  # Marked/rejected by money parser, preserved only as evidence.
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)[:200]


def midpoint(book: CapturedBook) -> Decimal | None:
    if not book.normalized.bids or not book.normalized.asks:
        return None
    with localcontext() as context:
        context.prec = 78
        return (book.normalized.bids[0].price + book.normalized.asks[0].price) / Decimal(2)


