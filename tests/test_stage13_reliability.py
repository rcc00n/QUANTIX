"""Local synthetic reliability checks. No connector or notification egress."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from arbiq.collector import worker
from arbiq.collector.config import CollectorConfig
from arbiq.collector.reliability import (
    CHECK_LABELS,
    CheckObservation,
    CollectorReliability,
    clock_trust,
    market_coverage,
    native_streams,
    scanner_progress,
)
from arbiq.collector.scheduling import Schedule
from arbiq.collector.worker import Collector, ExchangeRuntime
from arbiq.db.models import PaperSession, StrategyVersion, SystemEvent
from arbiq.notifications.models import NotificationOutbox
from arbiq.notifications.reports import schedule_reports
from arbiq.paper_trading.health import paper_valuation_health
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.models import PortfolioCheckpoint
from arbiq.settings.terminal import NotificationPreferences
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_collector_recovery import settings
from test_market_data import synthetic_capture
from test_notifications import synthetic_owner, synthetic_pair

NOW = datetime(2026, 1, 4, 16, 5, tzinfo=UTC)
HEALTHY = CheckObservation("HEALTHY", None, {})
DOWN = CheckObservation("DEGRADED", "SYNTHETIC_OUTAGE", {})
UNKNOWN = CheckObservation("UNKNOWN", "SYNTHETIC_GAP", {})


def observe(monitor, stamp, state, *, trusted=True):
    return monitor.observe(
        stamp,
        NOW + timedelta(seconds=stamp) if trusted else None,
        dict.fromkeys(CHECK_LABELS, state),
    )


def test_sustained_transitions_debounce_and_hold_across_missing_evidence():
    monitor = CollectorReliability()
    for stamp in range(0, 60, 5):
        assert not observe(monitor, stamp, DOWN)
    assert monitor.snapshot["checks"][0]["pending_seconds"] == 55
    assert len(observe(monitor, 60, DOWN)) == len(CHECK_LABELS)
    assert not observe(monitor, 65, DOWN)
    assert not observe(monitor, 70, UNKNOWN, trusted=False)
    assert monitor.snapshot["observed_at"] is None
    assert monitor.snapshot["checks"][0]["state"] == "UNKNOWN"
    for stamp in range(75, 105, 5):
        assert not observe(monitor, stamp, HEALTHY)
    recovery = observe(monitor, 105, HEALTHY)
    assert {item.key for item in recovery if item.state == "HEALTHY"} == set(
        CHECK_LABELS
    )
    assert not observe(monitor, 110, HEALTHY)


def test_monotonic_sample_gaps_and_observation_flaps_restart_pending_window():
    monitor = CollectorReliability()
    assert not observe(monitor, 0, DOWN)
    assert not observe(monitor, 60, DOWN)
    assert monitor.snapshot["checks"][0]["pending_seconds"] == 0
    assert not observe(monitor, 65, HEALTHY)
    assert not observe(monitor, 70, DOWN)
    assert monitor.snapshot["checks"][0]["pending_seconds"] == 0


def test_independent_market_sync_clock_and_queue_observations():
    counts = {"FRESH": 8, "AGING": 3, "STALE": 1, "UNAVAILABLE": 3}
    assert market_coverage(counts, clock_trusted=True).reason == "MARKET_COVERAGE_LOSS"
    unknown = market_coverage(counts, clock_trusted=False)
    assert (
        unknown.state == "UNKNOWN"
        and unknown.metrics["stale_or_unavailable_share"] is None
    )
    assert (
        clock_trust({"trusted": True, "host_healthy": False}).reason
        == "HOST_CLOCK_UNHEALTHY"
    )
    streams = dict(enabled=True, subscriptions={"kraken": 3, "okx": 1})
    assert native_streams(**streams, synchronized={"okx": 1}).metrics == {
        "subscribed_markets": 4,
        "synchronized_markets": 1,
        "unsynchronized_exchanges": ["kraken"],
    }
    assert (
        native_streams(**streams, synchronized={"kraken": 3, "okx": 1}).state
        == "HEALTHY"
    )
    args = dict(enabled=True, capacity=10, deferred=3, stale_seconds=30)
    assert scanner_progress(**args, ready=8, oldest=1, heartbeat_age=1).reason == (
        "SCANNER_QUEUE_SATURATION"
    )
    assert scanner_progress(**args, ready=2, oldest=30, heartbeat_age=1).reason == (
        "SCANNER_QUEUE_AGE_LIMIT"
    )
    assert scanner_progress(**args, ready=0, oldest=0, heartbeat_age=35).reason == (
        "SCANNER_HEARTBEAT_NOT_CURRENT"
    )


async def test_worker_reports_rest_freshness_independently_of_ws_and_clears_after_resync(
    monkeypatch,
):
    elapsed = 100.0
    monkeypatch.setattr(worker, "elapsed_seconds", lambda: elapsed)
    cfg = CollectorConfig(websocket_exchanges=["okx"])
    store = SimpleNamespace(run_id=uuid4(), event=AsyncMock())
    collector = Collector(settings(cfg), store)
    market = SimpleNamespace(id=uuid4())
    runtime = ExchangeRuntime(
        "okx", None, markets=[market], native_symbols={"BTC-USDT": market}
    )
    runtime.ws_task = SimpleNamespace(done=lambda: False)
    collector.runtimes["okx"] = runtime
    collector.scanner = SimpleNamespace(
        config=collector.settings.arbitrage,
        heartbeat=worker.time.monotonic(),
    )
    trusted = {"trusted": True, "host_healthy": True, "uncertainty_ms": 100}
    # Valid REST fallback keeps quote coverage healthy throughout the lost WS connection.
    for second in range(0, 100, 5):
        elapsed = 100 + second
        now = NOW + timedelta(seconds=second)
        collector.cache.books[market.id] = synthetic_capture(
            market_id=market.id, received_at=now
        )
        if second >= 65:
            runtime.native_synchronized = {market.id}
            runtime.ws_last_message_elapsed = elapsed
        await collector.sample_reliability(now, trusted, {"metrics": {}})
    assert [call.args[1] for call in store.event.await_args_list] == [
        "collector.reliability_degraded",
        "collector.reliability_recovered",
    ]
    assert all(
        call.args[-1]["check"] == "native_streams"
        for call in store.event.await_args_list
    )
    assert collector.reliability.snapshot["checks"][0]["state"] == "HEALTHY"


async def test_kraken_selection_change_retires_previous_transport_health(monkeypatch):
    class SyntheticKrakenStream:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, symbols, stop):
            await stop.wait()

    monkeypatch.setattr(worker, "KrakenStream", SyntheticKrakenStream)
    cfg = CollectorConfig(websocket_exchanges=["kraken"])
    store = SimpleNamespace(event=AsyncMock(), retain_runtime_keys=Mock())
    collector = Collector(settings(cfg), store)
    first, second = (
        SimpleNamespace(id=uuid4(), native_symbol=f"{base}/USD")
        for base in ("BTC", "ETH")
    )
    runtime = ExchangeRuntime(
        "kraken", None, markets=[first], schedules={first.id: Schedule()}
    )
    collector.runtimes["kraken"] = runtime
    runtime.ws_task = asyncio.create_task(collector.stop.wait())
    runtime.ws_synchronized = True
    runtime.ws_last_message_elapsed = worker.elapsed_seconds()
    runtime.ws_last[first.id] = runtime.ws_last_message_elapsed
    runtime.ws_published[first.id] = runtime.ws_last_message_elapsed
    try:
        await collector.apply_selection(runtime, [first, second])
        assert (
            runtime.ws_synchronized is False and runtime.ws_last_message_elapsed is None
        )
        assert not runtime.ws_last and not runtime.ws_published
    finally:
        runtime.ws_task.cancel()
        with suppress(asyncio.CancelledError):
            await runtime.ws_task


async def test_recorded_incident_is_queued_via_opted_in_paired_owner_report(
    database_url,
):
    engine = create_async_engine(database_url)
    session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session() as db:
            owner, other = await synthetic_owner(db), await synthetic_owner(db)
            binding = await synthetic_pair(db, owner, now=NOW - timedelta(hours=2))
            other_binding = await synthetic_pair(
                db, other, now=NOW - timedelta(hours=2)
            )
            other_binding.paused = True
            db.add(
                SystemEvent(
                    event_type="collector.reliability_degraded",
                    severity="warning",
                    message="SYNTHETIC collector reliability warning",
                    details={"check": "native_streams"},
                    created_at=NOW - timedelta(hours=1),
                )
            )
            await db.flush()
            await schedule_reports(db, binding, NotificationPreferences(), NOW)
            assert (
                await db.scalar(
                    select(NotificationOutbox.id).where(
                        NotificationOutbox.user_id == owner.id
                    )
                )
                is None
            )
            enabled = NotificationPreferences(telegram_enabled=True)
            await schedule_reports(db, binding, enabled, NOW)
            await schedule_reports(db, binding, enabled, NOW)
            await schedule_reports(db, other_binding, enabled, NOW)
            rows = (
                await db.scalars(
                    select(NotificationOutbox).where(
                        NotificationOutbox.user_id.in_((owner.id, other.id))
                    )
                )
            ).all()
            assert (
                len(rows) == 1
                and rows[0].user_id == owner.id
                and rows[0].binding_id == binding.id
            )
            assert rows[0].alert_id is None and rows[0].kind == "daily_report"
            assert (
                "Major system problems: collector.reliability_degraded"
                in rows[0].message
            )
    finally:
        await engine.dispose()


async def test_valuation_health_has_latest_owner_checkpoints_with_unknowns(
    database_url,
):
    engine = create_async_engine(database_url)
    session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session() as db:
            owner, other = await synthetic_owner(db), await synthetic_owner(db)
            for account, statuses in (
                (owner, ("ACTIVE", "ACTIVE", "PAUSED")),
                (other, ("ACTIVE",)),
            ):
                version = StrategyVersion(
                    user_id=account.id, version=1, settings={}, reason="SYNTHETIC"
                )
                db.add(version)
                await db.flush()
                for index, status in enumerate(statuses):
                    portfolio = PaperSession(
                        user_id=account.id,
                        strategy_version_id=version.id,
                        name="SYNTHETIC",
                        started_at=NOW,
                        initial_equity_cad=Decimal("10000"),
                        benchmark_snapshot={},
                    )
                    db.add(portfolio)
                    await db.flush()
                    db.add(
                        PaperControl(
                            session_id=portfolio.id,
                            strategy_version_id=version.id,
                            classification="VALIDATED",
                            mode="PRE_FUNDED",
                            status=status,
                            settings={},
                            updated_at=NOW,
                        )
                    )
                    if index != 1:
                        for age, state in ((10, "STALE"), (0, "PARTIAL")):
                            db.add(
                                PortfolioCheckpoint(
                                    session_id=portfolio.id,
                                    as_of=NOW - timedelta(seconds=age),
                                    payload={"valuation_status": state},
                                    calculation_version="SYNTHETIC",
                                )
                            )
            await db.flush()
            result = await paper_valuation_health(db, owner.id)
            assert result["portfolios_sampled"] == 2 and not result["truncated"]
            assert result["latest_checkpoint_at"] == NOW
            assert result["valuation_counts"] == {
                "COMPLETE_FRESH": 0,
                "PARTIAL": 1,
                "STALE": 0,
                "UNKNOWN": 1,
            }
    finally:
        await engine.dispose()
