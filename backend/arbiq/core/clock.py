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


