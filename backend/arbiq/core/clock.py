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


