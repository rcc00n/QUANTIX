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


def deviation_pct(value: Decimal, reference: Decimal) -> Decimal:
    if reference <= 0:
        raise ValueError("Reference must be positive")
    with localcontext() as context:
        context.prec = 78
        return (value - reference).copy_abs() / reference * Decimal(100)


def exchange_timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str) and "T" in value:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("Timezone missing")
        return stamp.astimezone(UTC)
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    raise ValueError("Invalid timestamp")


def validate_book(
    raw,
    *,
    market_id: UUID,
    exchange: str,
    symbol: str,
    base_key: str,
    quote_key: str,
    received_at: datetime,
    request_started_at: datetime,
    latency_ms: int | None,
    config: CollectorConfig,
    source: str = "rest",
    identity_verified: bool = True,
    active_verified: bool = True,
    clock_healthy: bool = True,
) -> CapturedBook:
    reasons: list[str] = []
    suspicious = False
    malformed = not isinstance(raw, dict)
    if malformed:
        raw = {}
        reasons.append("malformed_response")
    evidence = {
        "symbol": raw.get("symbol"),
        "timestamp_basis": raw.get("timestamp_basis", "exchange_field"),
        "timestamp": safe_json(raw.get("timestamp")),
        "bids": safe_json(raw.get("bids", [])[: config.depth])
        if isinstance(raw.get("bids"), list)
        else str(raw.get("bids"))[:200],
        "asks": safe_json(raw.get("asks", [])[: config.depth])
        if isinstance(raw.get("asks"), list)
        else str(raw.get("asks"))[:200],
    }
    if source.endswith("_ws") and isinstance(raw.get("stream_integrity"), dict):
        evidence["stream_integrity"] = safe_json(raw["stream_integrity"])
    if raw.get("symbol") not in (None, symbol):
        reasons.append("symbol_mismatch")
        suspicious = True
    if not identity_verified:
        reasons.append("unverified_asset_identity")
    if not active_verified:
        reasons.append("unverified_active_status")
    if not clock_healthy:
        reasons.append("collector_clock_jump")
        suspicious = True
    levels = {}
    for side in ("bids", "asks"):
        values = raw.get(side)
        parsed = []
        if not isinstance(values, list):
            values = []
            reasons.append(f"missing_{side}")
        for row in values[: config.depth]:
            try:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    raise ValueError("Invalid level")
                price, quantity = exact_decimal(row[0]), exact_decimal(row[1])
                if price <= 0 or quantity <= 0:
                    raise ValueError("Nonpositive level")
                parsed.append(BookLevel(price=price, quantity=quantity))
            except (ValueError, TypeError, ArithmeticError):
                reasons.append(f"invalid_{side}_level")
                suspicious = True
        if len(parsed) < config.minimum_levels:
            reasons.append(f"insufficient_{side}_depth")
        prices = [row.price for row in parsed]
        if any(
            (a <= b if side == "bids" else a >= b) for a, b in zip(prices, prices[1:], strict=False)
        ):
            reasons.append(f"unordered_{side}")
            suspicious = True
        levels[side] = tuple(parsed)
    if levels["bids"] and levels["asks"]:
        bid, ask = levels["bids"][0].price, levels["asks"][0].price
        if bid >= ask:
            reasons.append("crossed_or_locked_book")
            suspicious = True
        elif deviation_pct(ask, bid) > config.anomaly_deviation_pct:
            reasons.append("extreme_spread")
            suspicious = True
        with localcontext() as context:
            context.prec = 78
            notional = min(
                sum((v.price * v.quantity for v in levels["bids"]), Decimal(0)),
                sum((v.price * v.quantity for v in levels["asks"]), Decimal(0)),
            )
        if notional < config.minimum_book_notional:
            reasons.append("insufficient_quote_liquidity")
    timestamp = None
    source_timestamp = raw.get("timestamp")
    if source_timestamp is None:
        reasons.append("missing_exchange_timestamp")
    else:
        try:
            timestamp = exchange_timestamp(source_timestamp)
            age = (received_at - timestamp).total_seconds()
            if timestamp.year < 2009 or age < 0:
                reasons.append("invalid_or_future_timestamp")
                suspicious = True
            elif age > config.maximum_age_seconds:
                reasons.append("stale_exchange_timestamp")
        except (ValueError, TypeError, OverflowError, OSError):
            timestamp = None
            reasons.append("invalid_timestamp")
            suspicious = True
    if request_started_at > received_at:
        reasons.append("ingestion_clock_reversed")
        suspicious = True
    if not levels["bids"] or not levels["asks"] or malformed:
        state = DataState.UNAVAILABLE
    elif suspicious:
        state = DataState.SUSPICIOUS
    elif "stale_exchange_timestamp" in reasons:
        state = DataState.STALE
    elif reasons:
        state = DataState.DEGRADED
    else:
        state = DataState.HEALTHY
    quality = (
        "valid"
        if state == DataState.HEALTHY
        else "stale"
        if state == DataState.STALE
        else "invalid"
        if suspicious or malformed
        else "incomplete"
    )
    normalized = NormalizedOrderBook(
        market_id=market_id,
        observed_at=timestamp or received_at,
        received_at=received_at,
        bids=levels["bids"],
        asks=levels["asks"],
        quality=quality,
        source=source,
        sequence=str(raw["nonce"]) if raw.get("nonce") is not None else None,
    )
    digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CapturedBook(
        market_id=market_id,
        exchange=exchange,
        symbol=symbol,
        base_key=base_key,
        quote_key=quote_key,
        normalized=normalized,
        exchange_timestamp=timestamp,
        received_at=received_at,
        request_started_at=request_started_at,
        latency_ms=max(0, latency_ms) if latency_ms is not None else None,
        state=state,
        reasons=tuple(dict.fromkeys(reasons)),
        source=source,
        sequence=normalized.sequence,
        content_sha256=digest,
        raw_evidence=evidence,
    )


