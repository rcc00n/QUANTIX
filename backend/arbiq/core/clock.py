"""Application-scoped, externally checked UTC; never adjusts the operating system clock.

HTTPS time endpoints are independent corroborating sources, not atomic clocks. Their
precision, network uncertainty and agreement are explicit. An untrusted reading is
diagnostic only and must not establish financial freshness.
"""

import asyncio
import itertools
import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp


def elapsed_seconds() -> float:
    """Unadjusted monotonic time avoids NTP frequency slewing on Linux/WSL."""
    if hasattr(time, "CLOCK_MONOTONIC_RAW"):
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    return time.monotonic()


ELAPSED_SOURCE = "CLOCK_MONOTONIC_RAW" if hasattr(time, "CLOCK_MONOTONIC_RAW") else "monotonic"


@dataclass(frozen=True)
class TimeSource:
    name: str
    url: str
    path: tuple[str | int, ...]
    divisor: int
    resolution_seconds: float


TIME_SOURCES = (
    TimeSource("kraken", "https://api.kraken.com/0/public/Time", ("result", "unixtime"), 1, 1),
    TimeSource("coinbase", "https://api.exchange.coinbase.com/time", ("epoch",), 1, 0.001),
    TimeSource("binance", "https://api.binance.com/api/v3/time", ("serverTime",), 1000, 0.001),
    TimeSource("okx", "https://www.okx.com/api/v5/public/time", ("data", 0, "ts"), 1000, 0.001),
)


@dataclass(frozen=True)
class TimeSample:
    source: str
    utc_at_receipt: datetime
    elapsed_at_receipt: float
    uncertainty_seconds: float
    round_trip_seconds: float
    host_at_receipt: datetime
    source_timestamp: str

    def projected(self, elapsed: float) -> datetime:
        return self.utc_at_receipt + timedelta(seconds=elapsed - self.elapsed_at_receipt)

    def evidence(self) -> dict[str, Any]:
        value = asdict(self)
        value["utc_at_receipt"] = self.utc_at_receipt.isoformat()
        value["host_at_receipt"] = self.host_at_receipt.isoformat()
        return value


def parse_source_timestamp(payload: Any, source: TimeSource) -> tuple[datetime, str]:
    value = payload
    for part in source.path:
        value = value[part]
    if isinstance(value, (float, bool)):
        raise ValueError("inexact time representation")
    try:
        epoch = Decimal(str(value)) / source.divisor
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid time representation") from exc
    if not epoch.is_finite() or not Decimal("1577836800") <= epoch <= Decimal("4102444800"):
        raise ValueError("time outside supported 2020-2100 range")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(epoch * 1000000)), str(
        value
    )


async def fetch_time_sample(
    session: aiohttp.ClientSession,
    source: TimeSource,
    *,
    elapsed: Callable[[], float] = elapsed_seconds,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TimeSample:
    started = elapsed()
    async with session.get(
        source.url,
        params={"quantix_time_probe": str(time.monotonic_ns())},
        headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
        timeout=aiohttp.ClientTimeout(total=5),
        allow_redirects=False,
    ) as response:
        response.raise_for_status()
        if response.status != 200:
            raise ValueError("unexpected time endpoint status")
        if Decimal(response.headers.get("Age", "0")) > 0:
            raise ValueError("cached time response")
        raw = await response.content.read(16385)
        if len(raw) > 16384:
            raise ValueError("oversized time response")
        payload = json.loads(raw, parse_float=Decimal)
        timestamp, original = parse_source_timestamp(payload, source)
    ended = elapsed()
    rtt = ended - started
    if not math.isfinite(rtt) or not 0 <= rtt <= 3:
        raise ValueError("time response latency outside trust budget")
    # Generation occurs somewhere within the request interval. Truncated source
    # precision adds [0,resolution]; midpoint is an estimate, bounds include both.
    uncertainty = rtt / 2 + source.resolution_seconds / 2 + 0.05
    return TimeSample(
        source.name,
        timestamp + timedelta(seconds=rtt / 2 + source.resolution_seconds / 2),
        ended,
        uncertainty,
        rtt,
        wall(),
        original,
    )


class TrustedClock:
    """Fail-closed UTC projection anchored to corroborated independent HTTPS clocks.

    Call synchronize every 30 seconds. Two stable independent-source rounds are
    required. Host health is a separate flag: an externally anchored UTC clock does
    not falsely claim that the shared OS clock was repaired.
    """

    def __init__(
        self,
        *,
        elapsed: Callable[[], float] = elapsed_seconds,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_age_seconds: float = 90,
        maximum_uncertainty_seconds: float = 2,
        required_stable_rounds: int = 2,
    ):
        self.elapsed = elapsed
        self.wall = wall
        self.max_age_seconds = max_age_seconds
        self.maximum_uncertainty_seconds = maximum_uncertainty_seconds
        self.required_stable_rounds = required_stable_rounds
        self.anchor_utc: datetime | None = None
        self.anchor_elapsed = 0.0
        self.anchor_uncertainty = float("inf")
        self.stable_rounds = 0
        self.reason = "NO_EXTERNAL_REFERENCE"
        self.sources: list[dict[str, Any]] = []
        self.errors: dict[str, str] = {}
        self.rounds = 0
        self.wall_steps = 0
        self.last_wall_step_seconds: float | None = None
        self.last_wall_step_elapsed = float("-inf")
        self.previous_wall = self.wall()
        self.previous_elapsed = self.elapsed()
        self.started_elapsed = self.previous_elapsed
        self.stability_started_elapsed = self.previous_elapsed

    def observe_wall(self) -> None:
        now, wall = self.elapsed(), self.wall()
        movement = (wall - self.previous_wall).total_seconds() - (now - self.previous_elapsed)
        if abs(movement) > 1:
            self.wall_steps += 1
            self.last_wall_step_seconds = movement
            self.last_wall_step_elapsed = now
        self.previous_wall, self.previous_elapsed = wall, now

    def apply_samples(self, samples: list[TimeSample]) -> bool:
        """Pure evidence processing; synthetic samples can exercise every gate."""
        self.observe_wall()
        self.rounds += 1
        now = self.elapsed()
        valid = {
            sample.source: sample
            for sample in samples
            if sample.utc_at_receipt.tzinfo is not None
            and math.isfinite(sample.uncertainty_seconds)
            and 0 < sample.uncertainty_seconds <= self.maximum_uncertainty_seconds
            and math.isfinite(sample.round_trip_seconds)
            and 0 <= sample.round_trip_seconds <= 3
            and 0 <= now - sample.elapsed_at_receipt <= 10
        }
        # Find an unambiguous largest intersecting quorum. Equal-size conflicting
        # clusters do not select whichever happened to arrive first.
        quorums: list[tuple[tuple[TimeSample, ...], datetime, datetime]] = []
        for size in range(len(valid), 1, -1):
            for group in itertools.combinations(valid.values(), size):
                lower = max(
                    s.projected(now) - timedelta(seconds=s.uncertainty_seconds) for s in group
                )
                upper = min(
                    s.projected(now) + timedelta(seconds=s.uncertainty_seconds) for s in group
                )
                if lower <= upper:
                    quorums.append((group, lower, upper))
            if quorums:
                break
        if not quorums:
            self.stable_rounds = 0
            self.stability_started_elapsed = now
            self.reason = "INSUFFICIENT_AGREEING_REFERENCES"
            self.sources = [sample.evidence() for sample in valid.values()]
            return False
        lower = max(q[1] for q in quorums)
        upper = min(q[2] for q in quorums)
        if lower > upper:
            self.stable_rounds = 0
            self.stability_started_elapsed = now
            self.reason = "AMBIGUOUS_REFERENCE_QUORUM"
            self.sources = [sample.evidence() for sample in valid.values()]
            return False
        estimate = lower + (upper - lower) / 2
        uncertainty = (upper - lower).total_seconds() / 2
        prior = self.anchor_utc
        age = now - self.anchor_elapsed
        if prior is not None and 0 <= age <= self.max_age_seconds:
            predicted = prior + timedelta(seconds=age)
            disagreement = abs((estimate - predicted).total_seconds())
            tolerance = max(0.25, uncertainty + self.anchor_uncertainty + age * 0.0001)
            if disagreement > tolerance:
                self.stable_rounds = 0
                self.stability_started_elapsed = now
                self.reason = "ELAPSED_CLOCK_OR_REFERENCE_DISCONTINUITY"
        else:
            self.stable_rounds = 0
            self.stability_started_elapsed = now
        self.stable_rounds += 1
        self.anchor_utc, self.anchor_elapsed = estimate, now
        self.anchor_uncertainty = uncertainty
        members = {s.source for group, _, _ in quorums for s in group}
        self.sources = [sample.evidence() for name, sample in valid.items() if name in members]
        if self.trusted:
            self.reason = "EXTERNAL_UTC_VERIFIED"
        elif self.reason != "ELAPSED_CLOCK_OR_REFERENCE_DISCONTINUITY":
            self.reason = "AWAITING_STABILITY_ROUND"
        return self.trusted

    async def synchronize(self, session: aiohttp.ClientSession) -> bool:
        results = await asyncio.gather(
            *(
                fetch_time_sample(session, source, elapsed=self.elapsed, wall=self.wall)
                for source in TIME_SOURCES
            ),
            return_exceptions=True,
        )
        self.errors = {
            source.name: type(result).__name__
            for source, result in zip(TIME_SOURCES, results, strict=True)
            if isinstance(result, BaseException)
        }
        return self.apply_samples([result for result in results if isinstance(result, TimeSample)])

    @property
    def trusted(self) -> bool:
        age = self.elapsed() - self.anchor_elapsed
        return (
            self.anchor_utc is not None
            and self.stable_rounds >= self.required_stable_rounds
            and self.elapsed() - self.stability_started_elapsed >= 5
            and 0 <= age <= self.max_age_seconds
            and self.anchor_uncertainty + age * 0.0001 <= self.maximum_uncertainty_seconds
        )

    def utcnow(self) -> datetime:
        """Untrusted fallback is host UTC for diagnostics, never verification."""
        if self.anchor_utc is None:
            return self.wall()
        return self.anchor_utc + timedelta(seconds=self.elapsed() - self.anchor_elapsed)

    def snapshot(self) -> dict[str, Any]:
        self.observe_wall()
        now = self.elapsed()
        utc = self.utcnow()
        offset = (self.wall() - utc).total_seconds()
        trusted = self.trusted
        uncertainty = self.anchor_uncertainty + max(0, now - self.anchor_elapsed) * 0.0001
        reason = self.reason
        if self.anchor_utc is not None and now - self.anchor_elapsed > self.max_age_seconds:
            reason = "EXTERNAL_REFERENCE_EXPIRED"
        return {
            "trusted": trusted,
            "host_healthy": trusted
            and abs(offset) <= 2
            and now - self.started_elapsed >= 120
            and now - self.last_wall_step_elapsed > 120,
            "utc_now": utc.isoformat(),
            "host_utc": self.wall().isoformat(),
            "host_offset_seconds": round(offset, 6),
            "uncertainty_ms": round(uncertainty * 1000, 3) if math.isfinite(uncertainty) else None,
            "reference_age_seconds": round(now - self.anchor_elapsed, 3)
            if self.anchor_utc is not None
            else None,
            "stable_rounds": self.stable_rounds,
            "rounds": self.rounds,
            "wall_steps": self.wall_steps,
            "last_wall_step_seconds": self.last_wall_step_seconds,
            "elapsed_source": ELAPSED_SOURCE,
            "reason": reason,
            "sources": self.sources,
            "errors": self.errors,
        }
