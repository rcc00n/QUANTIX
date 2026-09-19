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


