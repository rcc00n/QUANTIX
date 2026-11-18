"""Deterministic synthetic IPC failure and recovery tests. No exchange calls."""

import asyncio
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from arbiq.opportunities import api


class Reply:
    def __init__(
        self, *, status=200, body=None, failure=None, entered=None, release=None
    ):
        self.status = status
        self.body = body
        self.failure = failure
        self.entered = entered
        self.release = release

    async def __aenter__(self):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Client:
    def __init__(self, *replies):
        self.replies = deque(replies)
        self.paths = []

    def get(self, path, **kwargs):
        self.paths.append((path, kwargs))
        return self.replies.popleft()


def request(app):
    return SimpleNamespace(state=SimpleNamespace(), app=app)


def db_fallback():
    return SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                metrics={"engine": {"healthy": True, "clock": {"trusted": True}}},
                heartbeat_at=datetime(2026, 11, 24, tzinfo=UTC),
            )
        )
    )


def empty_db():
    return SimpleNamespace(scalar=AsyncMock(return_value=None))


def healthy():
    return {
        "healthy": True,
        "clock": {
            "trusted": True,
            "host_healthy": True,
            "utc_now": "2026-11-24T00:00:00+00:00",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        Reply(failure=TimeoutError()),
        Reply(failure=aiohttp.ClientConnectionError()),
        Reply(status=503),
    ],
)
async def test_outage_uses_one_transport_attempt_per_request_and_closes_freshness_gate(
    monkeypatch, failure
):
    monkeypatch.setattr(api, "elapsed_seconds", lambda: 100)
    client = Client(failure)
    app = SimpleNamespace(state=SimpleNamespace(collector_http=client))
    first = request(app)
    assert await api.collector_current(first) == ({}, None)
    for item in (first, request(app)):
        result = await api.collector_diagnostics(item, db_fallback())
        assert result["source"] == "persisted_engine_diagnostics"
        assert result["clock"]["trusted"] is False
        assert not result["healthy"]
    assert client.paths == [("/engine/current", {"params": {"summary": "0"}})]


@pytest.mark.asyncio
async def test_only_one_recovery_probe_and_success_requires_new_live_payload(
    monkeypatch,
):
    tick = [100.0]
    monkeypatch.setattr(api, "elapsed_seconds", lambda: tick[0])
    entered, release = asyncio.Event(), asyncio.Event()
    client = Client(
        Reply(failure=TimeoutError()),
        Reply(
            body={"items": [{"id": "SYNTHETIC-A"}], "diagnostics": healthy()},
            entered=entered,
            release=release,
        ),
        Reply(body={"items": [{"id": "SYNTHETIC-B"}], "diagnostics": healthy()}),
    )
    app = SimpleNamespace(state=SimpleNamespace(collector_http=client))
    assert await api.collector_current(request(app)) == ({}, None)
    tick[0] += api.COLLECTOR_RETRY_SECONDS
    recovering = asyncio.create_task(api.collector_current(request(app)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    fallback = await api.collector_diagnostics(request(app), empty_db())
    assert fallback["clock"]["trusted"] is False
    assert len(client.paths) == 2
    release.set()
    items, health = await recovering
    assert set(items) == {"SYNTHETIC-A"}
    assert health["clock_healthy"] and health["source"] == "live_collector_engine"
    items, _ = await api.collector_current(request(app))
    assert set(items) == {"SYNTHETIC-B"}
    assert len(client.paths) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [Reply(status=401), Reply(body=ValueError("SYNTHETIC invalid JSON"))]
)
async def test_endpoint_errors_leave_diagnostics_accessible(failure):
    client = Client(failure, Reply(body=healthy()))
    app = SimpleNamespace(state=SimpleNamespace(collector_http=client))
    first = request(app)
    assert await api.collector_current(first) == ({}, None)
    result = await api.collector_diagnostics(first, empty_db())
    assert result["clock_healthy"]
    assert [path for path, _ in client.paths] == [
        "/engine/current",
        "/engine/diagnostics",
    ]


@pytest.mark.asyncio
async def test_cancelled_probe_releases_later_recovery(monkeypatch):
    tick = [100.0]
    monkeypatch.setattr(api, "elapsed_seconds", lambda: tick[0])
    entered, blocked = asyncio.Event(), asyncio.Event()
    client = Client(
        Reply(failure=TimeoutError()),
        Reply(entered=entered, release=blocked),
        Reply(body=healthy()),
    )
    app = SimpleNamespace(state=SimpleNamespace(collector_http=client))
    await api.collector_current(request(app))
    tick[0] += api.COLLECTOR_RETRY_SECONDS
    probe = asyncio.create_task(api.collector_current(request(app)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert (await api.collector_diagnostics(request(app), empty_db()))["clock_healthy"]


@pytest.mark.asyncio
async def test_late_concurrent_failure_cannot_reopen_recovered_ipc(monkeypatch):
    tick = [100.0]
    monkeypatch.setattr(api, "elapsed_seconds", lambda: tick[0])
    entered, release = asyncio.Event(), asyncio.Event()
    client = Client(
        Reply(failure=TimeoutError(), entered=entered, release=release),
        Reply(body=healthy()),
        Reply(body=healthy()),
    )
    app = SimpleNamespace(state=SimpleNamespace(collector_http=client))
    pending = asyncio.create_task(api.collector_current(request(app)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    tick[0] += 1
    assert (await api.collector_diagnostics(request(app), empty_db()))["clock_healthy"]
    release.set()
    assert await pending == ({}, None)
    assert (await api.collector_diagnostics(request(app), empty_db()))["clock_healthy"]
