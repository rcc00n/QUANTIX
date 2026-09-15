"""All UTC references and clock movements here are explicitly SYNTHETIC."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from arbiq.core.clock import (
    TIME_SOURCES,
    TimeSample,
    TrustedClock,
    fetch_time_sample,
    parse_source_timestamp,
)


class SyntheticTime:
    def __init__(self):
        self.elapsed = 1000.0
        self.wall = datetime(2026, 1, 1, tzinfo=UTC)

    def move(self, seconds, wall_seconds=None):
        self.elapsed += seconds
        self.wall += timedelta(
            seconds=seconds if wall_seconds is None else wall_seconds
        )

    def clock(self):
        return TrustedClock(elapsed=lambda: self.elapsed, wall=lambda: self.wall)

    def samples(self, offset=0, uncertainty=0.1):
        utc = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
            seconds=self.elapsed - 1000 + offset
        )
        return [
            TimeSample(
                name, utc, self.elapsed, uncertainty, 0.05, self.wall, "synthetic"
            )
            for name in ("synthetic-independent-a", "synthetic-independent-b")
        ]


def establish(time):
    clock = time.clock()
    assert not clock.apply_samples(time.samples())
    time.move(30)
    assert clock.apply_samples(time.samples())
    return clock


def test_clock_requires_multiple_independent_stable_rounds_and_expires():
    time = SyntheticTime()
    clock = establish(time)
    assert not clock.snapshot()["host_healthy"]  # Requires120s host observation.
    assert clock.snapshot()["elapsed_source"] in {"CLOCK_MONOTONIC_RAW", "monotonic"}
    time.move(90)
    assert clock.snapshot()["host_healthy"]
    time.move(1)
    assert not clock.trusted
    assert clock.snapshot()["reason"] == "EXTERNAL_REFERENCE_EXPIRED"
    assert not clock.snapshot()["host_healthy"]


def test_wall_step_does_not_reverse_verified_utc_and_host_is_gated():
    time = SyntheticTime()
    clock = establish(time)
    before = clock.utcnow()
    time.move(15, wall_seconds=-65)
    assert clock.utcnow() == before + timedelta(seconds=15)
    status = clock.snapshot()
    assert status["trusted"]
    assert status["wall_steps"] == 1
    assert status["last_wall_step_seconds"] == -80
    assert status["host_offset_seconds"] == -80
    assert not status["host_healthy"]






















class SyntheticResponse:
    status = 200

    def __init__(self, body=b'{"serverTime":1767225600123}', headers=None):
        self.body = body
        self.headers = headers or {}
        self.content = self

    async def read(self, size):
        return self.body[:size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None


class SyntheticSession:
    def __init__(self, response):
        self.response = response
        self.options = {}

    def get(self, url, **kwargs):
        self.options = kwargs
        return self.response






