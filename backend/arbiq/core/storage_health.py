"""Bounded PostgreSQL and scratch-volume telemetry with explicit observation scope."""

import asyncio
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from arbiq.analytics.full_export import MIN_FREE_BYTES
from arbiq.core.clock import elapsed_seconds

SAMPLE_SECONDS = 60
QUERY_BUDGET_SECONDS = 1.0
GROWTH_SPIKE_MIN_BYTES = 64 * 1024**2

_SNAPSHOT = text("""
WITH relations AS (
    SELECT relname AS name, pg_total_relation_size(relid) AS bytes,
           pg_indexes_size(relid) AS index_bytes, n_live_tup AS estimated_rows,
           seq_scan AS sequential_scans, idx_scan AS index_scans
    FROM pg_stat_user_tables
    WHERE schemaname = 'public' AND relname IN (
        'arbitrage_evaluations', 'order_book_snapshots', 'market_quality_events',
        'market_observations', 'portfolio_checkpoints', 'ledger_entries'
    )
), index_health AS (
    SELECT count(*) AS indexes,
           count(*) FILTER (WHERE NOT i.indisvalid OR NOT i.indisready) AS unhealthy_indexes
    FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid
    WHERE s.schemaname = 'public'
), visible_activity AS (
    SELECT count(*) FILTER (WHERE state IS NOT NULL) AS visible_sessions,
           count(*) FILTER (WHERE wait_event_type = 'Lock') AS visible_lock_waits,
           count(*) FILTER (WHERE state = 'active' AND
               query_start < statement_timestamp() - interval '30 seconds')
               AS visible_queries_over_30s
    FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid()
          AND backend_type = 'client backend'
)
SELECT statement_timestamp() AS observed_at, pg_postmaster_start_time() AS server_started_at,
       pg_database_size(current_database()) AS database_bytes, d.numbackends AS connections,
       d.temp_bytes, d.deadlocks, d.stats_reset AS database_stats_since,
       w.wal_bytes, w.stats_reset AS wal_stats_since,
       i.*, a.*,
       COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.bytes DESC, r.name)
                 FROM relations r), '[]'::jsonb) AS retained_relations
FROM pg_stat_database d CROSS JOIN pg_stat_wal w
CROSS JOIN index_health i CROSS JOIN visible_activity a
WHERE d.datname = current_database()
""")


@dataclass(frozen=True)
class StoragePoint:
    monotonic: float
    observed_at: datetime
    server_started_at: datetime
    database_bytes: int
    wal_bytes: int | None
    wal_stats_since: datetime | None


def _per_day(delta: int, seconds: float) -> int:
    return round(delta * 86_400 / seconds)


def storage_trend(points: list[StoragePoint]) -> dict:
    result = {
        "sample_count": len(points),
        "window_seconds": None,
        "window_started_at": None,
        "window_ended_at": None,
        "database_delta_bytes": None,
        "database_bytes_per_day": None,
        "wal_generated_bytes_per_day": None,
        "database_growth_spike": None,
    }
    if len(points) < 2:
        return result
    first, last = points[0], points[-1]
    elapsed = last.monotonic - first.monotonic
    if elapsed <= 0 or first.server_started_at != last.server_started_at:
        return result
    delta = last.database_bytes - first.database_bytes
    result.update(
        window_seconds=round(elapsed, 3),
        window_started_at=first.observed_at,
        window_ended_at=last.observed_at,
        database_delta_bytes=delta,
        database_bytes_per_day=_per_day(delta, elapsed),
    )
    wal = [point.wal_bytes for point in points if point.wal_bytes is not None]
    if (
        len(wal) == len(points)
        and all(point.wal_stats_since == last.wal_stats_since for point in points)
        and all(a <= b for a, b in zip(wal, wal[1:], strict=False))
    ):
        result["wal_generated_bytes_per_day"] = _per_day(wal[-1] - wal[0], elapsed)

    # Compare the latest material increase with the prior observed rate. A
    # handful of cold readings alone cannot establish a growth anomaly.
    if len(points) >= 7 and all(
        b.monotonic > a.monotonic for a, b in zip(points, points[1:], strict=False)
    ):
        previous = points[:-1]
        rates = [
            max(0, _per_day(b.database_bytes - a.database_bytes, b.monotonic - a.monotonic))
            for a, b in zip(previous, previous[1:], strict=False)
        ]
        latest_delta = last.database_bytes - previous[-1].database_bytes
        latest_elapsed = last.monotonic - previous[-1].monotonic
        result["database_growth_spike"] = latest_delta >= GROWTH_SPIKE_MIN_BYTES and _per_day(
            latest_delta, latest_elapsed
        ) > 2 * median(rates)
    return result


def _scratch_capacity() -> dict:
    try:
        size = shutil.disk_usage(tempfile.gettempdir())
    except OSError:
        return {"state": "UNAVAILABLE", "free_bytes": None, "reserve_bytes": MIN_FREE_BYTES}
    return {
        "state": "LOW_SPACE" if size.free < MIN_FREE_BYTES else "OBSERVED",
        "free_bytes": size.free,
        "total_bytes": size.total,
        "reserve_bytes": MIN_FREE_BYTES,
    }


async def _read_postgres(db) -> dict:
    # SAVEPOINT keeps a failed or cancelled diagnostics query from poisoning
    # the authenticated request's other read-only queries.
    async with asyncio.timeout(QUERY_BUDGET_SECONDS):
        async with db.begin_nested():
            return dict((await db.execute(_SNAPSHOT)).mappings().one())


class StorageHealth:
    def __init__(self, clock=elapsed_seconds):
        self.clock = clock
        self.lock = asyncio.Lock()
        self.sampled_at = None
        self.value = self._empty("NO_SAMPLE")
        self.points = deque(maxlen=61)

    @staticmethod
    def _empty(reason: str) -> dict:
        return {
            "schema_version": "quantix-storage-health-v1",
            "database_state": "UNAVAILABLE",
            "database_unavailable_reason": reason,
            "database": None,
            "database_growth": None,
            "database_volume_runway_days": None,
            "database_volume_runway_reason": "DATABASE_FILESYSTEM_FREE_SPACE_NOT_VISIBLE",
            "export_scratch": None,
            "backup_state": "UNAVAILABLE",
            "backup_unavailable_reason": "NO_VERIFIED_BACKUP_STATUS_SOURCE",
            "warnings": [],
            "sample_interval_seconds": SAMPLE_SECONDS,
        }

    async def sample(self, db) -> dict:
        now = self.clock()
        due = self.sampled_at is None or now - self.sampled_at >= SAMPLE_SECONDS
        if due and not self.lock.locked():
            async with self.lock:
                snapshot = self._empty("POSTGRES_STATS_UNAVAILABLE")
                disk = await asyncio.to_thread(_scratch_capacity)
                snapshot["export_scratch"] = disk
                if disk["state"] == "LOW_SPACE":
                    snapshot["warnings"].append("EXPORT_SCRATCH_BELOW_DISK_RESERVE")
                try:
                    row = await _read_postgres(db)
                except (TimeoutError, SQLAlchemyError):
                    pass
                else:
                    self._apply_snapshot(snapshot, row)
                self.value, self.sampled_at = snapshot, self.clock()
        return {
            **self.value,
            "sample_age_seconds": (
                round(max(0, self.clock() - self.sampled_at), 3)
                if self.sampled_at is not None
                else None
            ),
        }

    def _apply_snapshot(self, snapshot: dict, row: dict):
        previous = self.points[-1] if self.points else None
        now = self.clock()
        server_started = row.pop("server_started_at")
        wal_bytes = row["wal_bytes"]
        if previous is not None and (
            previous.server_started_at != server_started
            or now - previous.monotonic > 3 * SAMPLE_SECONDS
        ):
            self.points.clear()
        self.points.append(
            StoragePoint(
                now,
                row["observed_at"],
                server_started,
                row["database_bytes"],
                int(wal_bytes) if wal_bytes is not None else None,
                row["wal_stats_since"],
            )
        )
        row["wal_bytes"] = int(wal_bytes) if wal_bytes is not None else None
        trend = storage_trend(list(self.points))
        snapshot.update(
            database_state="OBSERVED",
            database_unavailable_reason=None,
            database=row,
            database_growth=trend,
        )
        if trend["database_growth_spike"]:
            snapshot["warnings"].append("UNUSUAL_DATABASE_GROWTH")
        if row["unhealthy_indexes"]:
            snapshot["warnings"].append("POSTGRES_INDEX_NOT_READY_OR_VALID")
