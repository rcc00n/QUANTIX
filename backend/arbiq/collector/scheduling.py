import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from arbiq.collector.config import CollectorConfig


def retry_delay(
    attempt: int,
    config: CollectorConfig,
    *,
    rate_limited: bool = False,
    jitter: float | None = None,
) -> float:
    base = max(30, config.retry_base_seconds) if rate_limited else config.retry_base_seconds
    value = min(config.retry_cap_seconds, base * 2 ** min(max(attempt - 1, 0), 12))
    return min(
        config.retry_cap_seconds,
        value * (1 + (random.random() if jitter is None else jitter) * 0.2),
    )


@dataclass
class Schedule:
    due: float = 0
    failures: int = 0

    def success(self, interval: float, *, started: float | None = None):
        self.failures = 0
        now = time.monotonic()
        self.due = now + interval if started is None else max(now, started + interval)

    def failure(self, config: CollectorConfig, error_code: str):
        self.failures += 1
        self.due = time.monotonic() + retry_delay(
            self.failures, config, rate_limited=error_code == "RATE_LIMIT"
        )


class ClockGuard:
    def __init__(self, skew_seconds: float = 5):
        self.last_wall = datetime.now(UTC)
        self.last_monotonic = time.monotonic()
        self.skew_seconds = skew_seconds
        self.healthy = True
        self.stable = 0

    def check(self, wall: datetime | None = None, monotonic: float | None = None) -> bool:
        wall = wall or datetime.now(UTC)
        monotonic = time.monotonic() if monotonic is None else monotonic
        drift = abs((wall - self.last_wall).total_seconds() - (monotonic - self.last_monotonic))
        if drift > self.skew_seconds:
            self.healthy = False
            self.stable = 0
        else:
            self.stable += 1
            if self.stable >= 5:
                self.healthy = True
        self.last_wall, self.last_monotonic = wall, monotonic
        return self.healthy
