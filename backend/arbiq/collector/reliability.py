"""Sustained collector health and recovery, independent of trading gates."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

DEGRADED_AFTER_SECONDS = 60
RECOVERED_AFTER_SECONDS = 30
MAX_SAMPLE_GAP_SECONDS = 20
MARKET_LOSS_SHARE = 0.25
SCANNER_QUEUE_SHARE = 0.80
SCANNER_HEARTBEAT_TIMEOUT_SECONDS = 30

CheckState = Literal["UNKNOWN", "HEALTHY", "DEGRADED", "DISABLED"]

CHECK_LABELS = {
    "market_coverage": "Market data coverage",
    "scanner_progress": "Opportunity scanner",
    "native_streams": "Native WebSocket synchronization",
    "clock_trust": "Collector clock",
}


@dataclass(frozen=True, slots=True)
class CheckObservation:
    state: CheckState
    reason: str | None
    metrics: dict


@dataclass(frozen=True, slots=True)
class CheckTransition:
    key: str
    state: CheckState
    reason: str | None


@dataclass(slots=True)
class _Check:
    state: CheckState = "UNKNOWN"
    pending_state: CheckState | None = None
    pending_since: float = 0
    reason: str | None = None


class CollectorReliability:
    def __init__(self):
        self._checks = {key: _Check() for key in CHECK_LABELS}
        self._last_sample: float | None = None
        self.snapshot = None

    def observe(
        self,
        stamp: float,
        utc: datetime | None,
        observations: dict[str, CheckObservation],
    ) -> tuple[CheckTransition, ...]:
        """Confirm continuous observations, using monotonic duration across samples."""
        continuous = self._last_sample is not None and (
            0 <= stamp - self._last_sample <= MAX_SAMPLE_GAP_SECONDS
        )
        self._last_sample = stamp
        transitions = []
        checks = []
        for key, check in self._checks.items():
            observation = observations[key]
            state = observation.state
            if state in {"UNKNOWN", "DISABLED"} or state == check.state:
                check.pending_state = None
                if state == check.state:
                    check.reason = observation.reason
            else:
                if not continuous or check.pending_state != state:
                    check.pending_state = state
                    check.pending_since = stamp
                required = (
                    DEGRADED_AFTER_SECONDS if state == "DEGRADED" else RECOVERED_AFTER_SECONDS
                )
                if stamp - check.pending_since >= required:
                    if state == "DEGRADED" or check.state == "DEGRADED":
                        transitions.append(CheckTransition(key, state, observation.reason))
                    check.state = state
                    check.reason = observation.reason
                    check.pending_state = None
            active = state in {"HEALTHY", "DEGRADED"}
            checks.append(
                {
                    "key": key,
                    "state": check.state if active else state,
                    "reason": (check.reason or observation.reason)
                    if active
                    else observation.reason,
                    "pending_state": check.pending_state,
                    "pending_seconds": round(stamp - check.pending_since, 3)
                    if check.pending_state is not None
                    else None,
                    "metrics": observation.metrics,
                }
            )
        self.snapshot = {
            "schema_version": "quantix-collector-reliability-v1",
            "observed_at": utc.isoformat() if utc else None,
            "checks": checks,
        }
        return tuple(transitions)


def market_coverage(counts: dict[str, int], *, clock_trusted: bool) -> CheckObservation:
    monitored = sum(counts.values())
    stale = counts.get("STALE", 0)
    unavailable = counts.get("UNAVAILABLE", 0)
    uncertain = counts.get("CLOCK_UNTRUSTED", 0)
    share = (stale + unavailable) / monitored if monitored and clock_trusted else None
    metrics = {
        "monitored_books": monitored,
        "fresh_books": counts.get("FRESH", 0),
        "aging_books": counts.get("AGING", 0),
        "stale_books": stale,
        "unavailable_books": unavailable,
        "clock_untrusted_books": uncertain,
        "stale_or_unavailable_share": share,
    }
    if not clock_trusted:
        return CheckObservation("UNKNOWN", "BOOK_AGE_CLOCK_UNTRUSTED", metrics)
    if not monitored:
        return CheckObservation("DEGRADED", "NO_MONITORED_MARKETS", metrics)
    if share >= MARKET_LOSS_SHARE:
        return CheckObservation("DEGRADED", "MARKET_COVERAGE_LOSS", metrics)
    if uncertain / monitored >= MARKET_LOSS_SHARE:
        return CheckObservation("DEGRADED", "BOOK_TIMESTAMP_INTEGRITY_DEGRADED", metrics)
    return CheckObservation("HEALTHY", None, metrics)


def scanner_progress(
    *,
    enabled: bool,
    ready: int,
    capacity: int,
    oldest: float,
    heartbeat_age: float | None,
    deferred: int,
    stale_seconds: float,
) -> CheckObservation:
    metrics = {
        "pending_comparisons": ready,
        "queue_capacity": capacity,
        "oldest_seconds": oldest,
        "heartbeat_age_seconds": heartbeat_age,
        "deferred_markets": deferred,
    }
    if not enabled:
        return CheckObservation("DISABLED", "SCANNER_DISABLED", metrics)
    if heartbeat_age is None or not 0 <= heartbeat_age < SCANNER_HEARTBEAT_TIMEOUT_SECONDS:
        return CheckObservation("DEGRADED", "SCANNER_HEARTBEAT_NOT_CURRENT", metrics)
    if capacity and ready >= capacity * SCANNER_QUEUE_SHARE:
        return CheckObservation("DEGRADED", "SCANNER_QUEUE_SATURATION", metrics)
    if oldest >= stale_seconds:
        return CheckObservation("DEGRADED", "SCANNER_QUEUE_AGE_LIMIT", metrics)
    return CheckObservation("HEALTHY", None, metrics)


def native_streams(
    *, enabled: bool, subscriptions: dict[str, int], synchronized: dict[str, int]
) -> CheckObservation:
    subscribed = sum(subscriptions.values())
    lost = sorted(
        slug for slug, count in subscriptions.items() if synchronized.get(slug, 0) < count
    )
    metrics = {
        "subscribed_markets": subscribed,
        "synchronized_markets": sum(synchronized.values()),
        "unsynchronized_exchanges": lost,
    }
    if not enabled:
        return CheckObservation("DISABLED", "NATIVE_STREAMS_DISABLED", metrics)
    if not subscribed:
        return CheckObservation("UNKNOWN", "NO_NATIVE_SUBSCRIPTIONS", metrics)
    if lost:
        return CheckObservation("DEGRADED", "NATIVE_STREAM_SYNCHRONIZATION_LOST", metrics)
    return CheckObservation("HEALTHY", None, metrics)


def clock_trust(clock: dict) -> CheckObservation:
    metrics = {"trusted": clock.get("trusted"), "host_healthy": clock.get("host_healthy")}
    if clock.get("trusted") is not True:
        return CheckObservation("DEGRADED", "TRUSTED_UTC_UNAVAILABLE", metrics)
    if clock.get("host_healthy") is not True:
        return CheckObservation("DEGRADED", "HOST_CLOCK_UNHEALTHY", metrics)
    return CheckObservation("HEALTHY", None, metrics)
