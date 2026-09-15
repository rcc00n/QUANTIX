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


def test_elapsed_clock_discontinuity_requires_reestablishment():
    time = SyntheticTime()
    clock = establish(time)
    time.move(30)
    assert not clock.apply_samples(time.samples(offset=-80))
    assert clock.snapshot()["reason"] == "ELAPSED_CLOCK_OR_REFERENCE_DISCONTINUITY"
    time.move(30)
    assert clock.apply_samples(time.samples(offset=-80))
    assert not clock.snapshot()["host_healthy"]


def test_missing_and_single_source_fail_closed_even_after_prior_trust():
    time = SyntheticTime()
    clock = establish(time)
    assert not clock.apply_samples(time.samples()[:1])
    assert not clock.trusted
    assert clock.snapshot()["reason"] == "INSUFFICIENT_AGREEING_REFERENCES"
    assert not clock.apply_samples([])


def test_duplicates_do_not_count_as_independent_sources():
    time = SyntheticTime()
    clock = time.clock()
    sample = time.samples()[0]
    assert not clock.apply_samples([sample, sample, sample])


def test_conflicting_equal_quorums_fail_closed():
    time = SyntheticTime()
    clock = time.clock()
    samples = time.samples()
    samples.extend(
        TimeSample(
            name,
            time.wall + timedelta(seconds=80),
            time.elapsed,
            0.1,
            0.05,
            time.wall,
            "synthetic-outlier",
        )
        for name in ("synthetic-independent-c", "synthetic-independent-d")
    )
    assert not clock.apply_samples(samples)
    assert clock.snapshot()["reason"] == "AMBIGUOUS_REFERENCE_QUORUM"


def test_single_outlier_does_not_defeat_independent_majority():
    time = SyntheticTime()
    clock = time.clock()
    samples = time.samples()
    samples.append(
        TimeSample(
            "synthetic-c", time.wall, time.elapsed, 0.1, 0.05, time.wall, "synthetic"
        )
    )
    samples.append(
        TimeSample(
            "synthetic-d",
            time.wall + timedelta(seconds=80),
            time.elapsed,
            0.1,
            0.05,
            time.wall,
            "synthetic-outlier",
        )
    )
    assert not clock.apply_samples(samples)
    assert {s["source"] for s in clock.sources} == {
        "synthetic-independent-a",
        "synthetic-independent-b",
        "synthetic-c",
    }


@pytest.mark.parametrize("uncertainty", [0, -1, 3, float("nan"), float("inf")])
def test_invalid_or_excessive_uncertainty_is_not_trusted(uncertainty):
    time = SyntheticTime()
    assert not time.clock().apply_samples(time.samples(uncertainty=uncertainty))


def test_old_or_future_received_samples_do_not_establish_time():
    time = SyntheticTime()
    samples = time.samples()
    time.move(11)
    assert not time.clock().apply_samples(samples)
    time.move(-20)
    assert not time.clock().apply_samples(samples)


def test_time_parsing_preserves_exact_fractional_epoch():
    utc, original = parse_source_timestamp(
        {"epoch": Decimal("1767225600.123456")}, TIME_SOURCES[1]
    )
    assert utc == datetime(2026, 1, 1, microsecond=123456, tzinfo=UTC)
    assert original == "1767225600.123456"
    assert parse_source_timestamp({"serverTime": "1767225600123"}, TIME_SOURCES[2])[
        0
    ] == (datetime(2026, 1, 1, microsecond=123000, tzinfo=UTC))


@pytest.mark.parametrize(
    "value", [1767225600.0, True, "NaN", "Infinity", "0", "999999999999"]
)
def test_invalid_time_payloads_rejected(value):
    with pytest.raises(ValueError):
        parse_source_timestamp({"epoch": value}, TIME_SOURCES[1])


def test_rapid_duplicate_rounds_do_not_establish_stability():
    time = SyntheticTime()
    clock = time.clock()
    for _ in range(10):
        assert not clock.apply_samples(time.samples())
    assert clock.snapshot()["reason"] == "AWAITING_STABILITY_ROUND"


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


@pytest.mark.asyncio
async def test_public_time_transport_preserves_roundtrip_bounds_and_disables_redirects():
    readings = iter([1000, 1000.2])
    session = SyntheticSession(SyntheticResponse())
    sample = await fetch_time_sample(
        session,
        TIME_SOURCES[2],
        elapsed=lambda: next(readings),
        wall=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert sample.utc_at_receipt == datetime(2026, 1, 1, microsecond=223500, tzinfo=UTC)
    assert sample.uncertainty_seconds == pytest.approx(0.1505)
    assert sample.round_trip_seconds == pytest.approx(0.2)
    assert session.options["allow_redirects"] is False
    assert session.options["timeout"].total == 5


@pytest.mark.parametrize(
    ("response", "readings"),
    [
        (SyntheticResponse(headers={"Age": "10"}), [1000, 1000.2]),
        (SyntheticResponse(body=b"x" * 16385), [1000, 1000.2]),
        (SyntheticResponse(), [1000, 1004]),
        (SyntheticResponse(), [1000, 999]),
    ],
)
@pytest.mark.asyncio
async def test_public_time_transport_rejects_cache_oversize_and_impossible_latency(
    response, readings
):
    elapsed = iter(readings)
    with pytest.raises(ValueError):
        await fetch_time_sample(
            SyntheticSession(response), TIME_SOURCES[2], elapsed=lambda: next(elapsed)
        )


@pytest.mark.asyncio
async def test_reference_errors_are_sanitized_and_fail_closed(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise TimeoutError("SYNTHETIC SECRET-LIKE STRING MUST NEVER BE REPORTED")

    monkeypatch.setattr("arbiq.core.clock.fetch_time_sample", unavailable)
    clock = SyntheticTime().clock()
    assert not await clock.synchronize(None)
    assert set(clock.snapshot()["errors"].values()) == {"TimeoutError"}
    assert "SECRET" not in str(clock.snapshot())
