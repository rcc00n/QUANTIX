"""Pure synthetic checks for the disk growth estimate and scope controls."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def load_module(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNWAY = load_module("retention_runway")


def sample(hour, size, scope="SYNTHETIC"):
    clock = datetime(2026, 7, 1, tzinfo=UTC) + timedelta(hours=hour)
    return {
        "observed_at": clock.isoformat(),
        "database_bytes": size,
        "evaluation_bytes": 0,
        "book_snapshot_bytes": 0,
        "ledger_rows": 7,
        "evaluation_rows": 12,
        "database_scope": scope,
    }


def test_runway_reserves_filesystem_space_and_does_not_extrapolate_flat_db():
    result = RUNWAY.estimate(
        [sample(0, 100), sample(1, 110)], free_bytes=500, reserve_bytes=20
    )
    assert result["linear_database_bytes_per_day"] == "240"
    assert result["linear_database_only_runway_days"] == "2"
    assert result["linear_30_day_database_growth_bytes"] == "7200"
    assert result["observed_deltas"]["ledger_rows"] == 0
    flat = RUNWAY.estimate(
        [sample(0, 100), sample(1, 100)], free_bytes=20, reserve_bytes=20
    )
    assert flat["linear_database_only_runway_days"] is None


@pytest.mark.parametrize("second", [sample(0, 100), sample(1, 110, "OTHER_DATABASE")])
def test_runway_rejects_unordered_or_incompatible_sources(second):
    with pytest.raises(ValueError):
        RUNWAY.estimate([sample(0, 100), second])


def test_postgresql_trimmed_microseconds_work_on_operational_python():
    second = sample(1, 102)
    second["observed_at"] = "2026-07-01T01:00:00.72207+00:00"
    result = RUNWAY.estimate([sample(0, 100), second])
    assert result["last_at"] == "2026-07-01T01:00:00.722070+00:00"
