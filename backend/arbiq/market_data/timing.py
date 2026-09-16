"""Bounded observational telemetry. Floats here measure time, never financial values."""

import math
from collections import deque
from datetime import datetime

from arbiq.core.clock import elapsed_seconds

TIMING_EXPORT_NOTES = (
    "\n\n## Market pipeline timing (Stage 8A)\n\n"
    "Book verification.pipeline preserves collector_received_at, collector_processed_at, "
    "cache_updated_at and instrumented HTTP/body/adapter timestamps. exchange_timestamp "
    "remains null when absent; normalized.observed_at may use receipt as a legacy fallback. "
    "request_started_at precedes local queue/pacing; request_ms is the complete CCXT call, "
    "http_body_ms ends at complete response text, and adapter_parse_ms ends at adapter return. "
    "Local durations use monotonic RAW (monotonic fallback), in milliseconds; UTC timestamps "
    "come from collector/worker TrustedClock and retain separate uncertainty/health. "
    "Actual live post-commit persisted_at is not retroactively invented inside DB payloads. "
    "Evaluation evidence.timing pins buy/sell receipt/source ages, synchronization differences "
    "and uncertainty, FX graph evidence age, and route evaluation duration. These are "
    "historical decision measurements, never a current quote or realized profit. "
    "Source-to-evaluation is the older supplied book time to completion; receive age is separate. "
    "Collector/scanner histogram windows are bounded; p95 requires20 samples, p99 requires100. "
    "Notification queue_requested_at is a batch reference; queue_insert_observed_at, when "
    "present, is a fresh worker-clock enqueue-statement observation, not commit time. "
    "generation_ms measures report preparation; request_duration_ms ends at provider HTTP "
    "return, before subsequent DB work; provider_acknowledged_at is not device receipt. "
    "Legacy latency_ms is not proof of pure queue/network waiting. Unknown/uninstrumented "
    "timing remains null/absent; no historical timing or missing source timestamp is fabricated.\n"
)


def distribution(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v) and v >= 0)
    count = len(values)

    def percentile(p):
        return round(values[max(0, math.ceil(count * p) - 1)], 3) if count else None

    return {
        "sample_count": count,
        "p50": percentile(0.5),
        "p95": percentile(0.95) if count >= 20 else None,
        "p99": percentile(0.99) if count >= 100 else None,
        "maximum": round(values[-1], 3) if count else None,
    }


class Timings:
    def __init__(self, limit=600):
        self.limit = limit
        self.values = {}

    def add(self, name, value):
        if value is not None and math.isfinite(value) and value >= 0:
            self.values.setdefault(name, deque(maxlen=self.limit)).append(
                (elapsed_seconds(), value)
            )

    def snapshot(self):
        now = elapsed_seconds()
        return {
            name: {
                **distribution(v for _, v in rows),
                "unit": "ms",
                "window_seconds": round(now - rows[0][0], 3),
                "window_limit": self.limit,
                "elapsed_source": "monotonic_raw_or_monotonic",
            }
            for name, rows in self.values.items()
            if rows
        }


def age_seconds(now: datetime, timestamp: datetime | None):
    """A future or unavailable timestamp is not a zero-age observation."""
    if timestamp is None:
        return None
    age = (now - timestamp).total_seconds()
    return round(age, 6) if age >= 0 else None


def freshness(book, now, config, clock):
    receive_age = age_seconds(now, book.received_at)
    source_age = age_seconds(now, book.exchange_timestamp)
    uncertainty_ms = clock.get("uncertainty_ms")
    try:
        uncertainty = float(uncertainty_ms) / 1000
        if not math.isfinite(uncertainty) or not 0 <= uncertainty <= 2:
            raise ValueError
    except (ValueError, TypeError):
        uncertainty = None
    reason = None
    if not clock.get("trusted") or uncertainty is None:
        state, reason = "CLOCK_UNTRUSTED", "UTC_REFERENCE_UNAVAILABLE"
    elif not book.normalized.bids or not book.normalized.asks:
        state, reason = "UNAVAILABLE", "TWO_SIDED_BOOK_UNAVAILABLE"
    elif receive_age is None or (book.exchange_timestamp and source_age is None):
        state, reason = "CLOCK_UNTRUSTED", "FUTURE_OBSERVATION"
    elif book.state.value == "STALE":
        # The live worker may have stricter owner-applied limits than API defaults.
        state, reason = "STALE", "BOOK_ALREADY_CLASSIFIED_STALE"
    else:
        # Include capture uncertainty as well as the current reference bound.
        captured = book.verification.get("clock", {}).get("uncertainty_ms")
        try:
            captured = float(captured) / 1000
            if not math.isfinite(captured) or not 0 <= captured <= 2:
                raise ValueError
        except (ValueError, TypeError):
            captured = 2.0
        upper = max(receive_age, source_age or 0) + uncertainty + captured
        stale_after = min(config.freshness_stale_seconds, config.maximum_age_seconds)
        state = (
            "STALE"
            if upper > stale_after
            else "AGING"
            if upper > config.freshness_fresh_seconds
            else "FRESH"
        )
        if source_age is None:
            reason = "SOURCE_TIMESTAMP_UNAVAILABLE_RECEIPT_AGE_ONLY"
    return {
        "classification": state,
        "reason": reason,
        "collector_age_seconds": receive_age,
        "source_age_seconds": source_age,
        "source_timestamp_supplied": book.exchange_timestamp is not None,
        "timestamp_basis": book.raw_evidence.get("timestamp_basis"),
        "fresh_seconds": config.freshness_fresh_seconds,
        "stale_seconds": min(config.freshness_stale_seconds, config.maximum_age_seconds),
        "clock_uncertainty_ms": uncertainty_ms,
    }
