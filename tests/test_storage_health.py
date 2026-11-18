"""SYNTHETIC observation and failure cases plus an isolated PostgreSQL request."""
# ruff: noqa: F811 -- Pytest discovers the isolated fixture by its imported name.

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from arbiq.core import storage_health
from arbiq.core.storage_health import (
    GROWTH_SPIKE_MIN_BYTES,
    StorageHealth,
    StoragePoint,
)
from arbiq.core.telemetry import CgroupTelemetry
from sqlalchemy.exc import SQLAlchemyError
from test_paper_api import private_api  # noqa: F401

START = datetime(2026, 7, 1, tzinfo=UTC)


def point(second, db_size, wal=0, *, restart=0, reset=0):
    return StoragePoint(
        second,
        START + timedelta(seconds=second),
        START + timedelta(seconds=restart),
        db_size,
        wal,
        START + timedelta(seconds=reset),
    )


def test_database_and_wal_windows_remain_independent_after_wal_reset():
    trend = storage_health.storage_trend([point(0, 100, 10), point(60, 103, 22)])
    assert trend["database_bytes_per_day"] == 4_320
    assert trend["wal_generated_bytes_per_day"] == 17_280
    assert trend["sample_count"] == 2
    assert trend["database_growth_spike"] is None
    reset = storage_health.storage_trend(
        [point(0, 100, 10), point(60, 103, 23, reset=60)]
    )
    assert reset["database_bytes_per_day"] == 4_320
    assert reset["wal_generated_bytes_per_day"] is None
    restarted = storage_health.storage_trend(
        [point(0, 100), point(60, 120, restart=60)]
    )
    assert restarted["database_bytes_per_day"] is None
    assert restarted["wal_generated_bytes_per_day"] is None


def test_growth_spike_compares_material_increase_to_previous_observed_baseline():
    prior = [point(60 * i, 1000 + i) for i in range(6)]
    below = storage_health.storage_trend([*prior, point(360, GROWTH_SPIKE_MIN_BYTES)])
    above = storage_health.storage_trend(
        [*prior, point(360, 1005 + GROWTH_SPIKE_MIN_BYTES)]
    )
    assert below["database_growth_spike"] is False
    assert above["database_growth_spike"] is True


def test_scratch_warning_uses_export_reserve_and_leaves_backup_health_unknown(
    monkeypatch,
):
    minimum = storage_health.MIN_FREE_BYTES
    monkeypatch.setattr(
        storage_health.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(
            free=minimum - 1,
            total=minimum + 1,
        ),
    )
    assert storage_health._scratch_capacity() == {
        "state": "LOW_SPACE",
        "free_bytes": minimum - 1,
        "total_bytes": minimum + 1,
        "reserve_bytes": minimum,
    }


async def test_unavailable_storage_does_not_invent_runway_or_duplicate_queries(
    monkeypatch,
):
    scheduled = asyncio.Event()
    release = asyncio.Event()

    async def failed_query(_):
        scheduled.set()
        await release.wait()
        raise SQLAlchemyError("PRIVATE TRANSPORT DETAILS")

    sample_query = AsyncMock(side_effect=failed_query)
    monkeypatch.setattr(storage_health, "_read_postgres", sample_query)
    monkeypatch.setattr(
        storage_health, "_scratch_capacity", lambda: {"state": "LOW_SPACE"}
    )
    health = StorageHealth(lambda: 0)
    first = asyncio.create_task(health.sample(None))
    await scheduled.wait()
    concurrent = await health.sample(None)
    release.set()
    result = await first
    assert (await health.sample(None)) == result
    assert sample_query.await_count == 1
    assert concurrent["database_state"] == "UNAVAILABLE"
    assert result["database"] is None
    assert result["backup_state"] == "UNAVAILABLE"
    assert result["database_volume_runway_days"] is None
    assert result["warnings"] == ["EXPORT_SCRATCH_BELOW_DISK_RESERVE"]
    assert "PRIVATE TRANSPORT DETAILS" not in str(result)


def test_cgroup_capacity_and_throttling_use_cgroup_counters(tmp_path):
    for name, value in {
        "memory.current": "768",
        "memory.max": "1024",
        "cpu.max": "80000 100000",
        "memory.events": "low 0\nmax 7\noom_kill 1\n",
        "cpu.stat": "usage_usec 900000\nnr_periods 10\nnr_throttled 4\n",
    }.items():
        (tmp_path / name).write_text(value)
    telemetry = CgroupTelemetry(tmp_path)
    first = telemetry.sample(0)
    assert first["memory_pct_of_limit"] == 75
    assert first["memory_limit_events_total"] == 7
    assert first["oom_kills_total"] == 1
    assert first["cpu_limit_cores"] == 0.8
    assert first["cpu_pct_of_limit"] is None
    (tmp_path / "cpu.stat").write_text(
        "usage_usec 1700000\nnr_periods 30\nnr_throttled 9\n"
    )
    second = telemetry.sample(2)
    assert second["cpu_pct_of_limit"] == 50
    assert second["cpu_throttled_period_pct"] == 25
    assert telemetry.sample(2.3) is second


def test_terminal_health_reads_a_bounded_postgres_snapshot(private_api):
    client, _, _ = private_api
    first = client.get("/api/v1/terminal/health")
    second = client.get("/api/v1/terminal/health")
    assert first.status_code == second.status_code == 200
    status = first.json()["storage"]
    assert status["database_state"] == "OBSERVED"
    database = status["database"]
    assert database["database_bytes"] > 0
    assert database["wal_bytes"] >= 0
    assert database["indexes"] > 0
    assert any(
        row["name"] == "ledger_entries" for row in database["retained_relations"]
    )
    assert (
        second.json()["storage"]["database"]["observed_at"] == database["observed_at"]
    )
    assert status["database_volume_runway_days"] is None
    assert status["backup_state"] == "UNAVAILABLE"
