"""SYNTHETIC pool-lifetime integration; only the dedicated _test DB is used."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from arbiq.auth.dependencies import DB, current_user
from arbiq.db.models import ArbitrageRoute
from arbiq.db.session import get_db
from arbiq.opportunities import api
from arbiq.opportunities.config import EngineConfig
from fastapi import FastAPI
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_opportunity_api import NOW, seed_routes, synthetic_health, synthetic_payload


@pytest.fixture
async def single_connection_radar(database_url, monkeypatch):
    seeded_ids = await seed_routes(database_url)
    engine = create_async_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
        hide_parameters=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(arbitrage=EngineConfig())
    app.state.collector_http = None
    route_queries = []
    route_parameters = []

    def record_route_queries(_connection, _cursor, statement, parameters, _context, _many):
        if "arbitrage_routes" in statement:
            route_queries.append(statement)
            route_parameters.append(parameters)

    event.listen(engine.sync_engine, "before_cursor_execute", record_route_queries)

    async def request_db():
        async with sessions() as db:
            yield db

    async def synthetic_owner(db: DB):
        # Real authentication also starts a read transaction on this same request
        # session. Keep that scarce lease in this fixture until the endpoint acts.
        await db.scalar(text("SELECT 1"))
        assert db.in_transaction()
        return SimpleNamespace(id=uuid4())

    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[current_user] = synthetic_owner
    monkeypatch.setattr(api, "_projection_gate", asyncio.Semaphore(1))
    monkeypatch.setattr(api, "_view_gate", asyncio.Semaphore(3))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://synthetic-test",
        ) as client:
            yield SimpleNamespace(
                client=client,
                engine=engine,
                sessions=sessions,
                route_queries=route_queries,
                route_parameters=route_parameters,
                seeded_ids=seeded_ids,
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_route_queries)
        await engine.dispose()


async def test_list_releases_auth_query_and_fallback_leases_before_ipc_and_cpu(
    single_connection_radar,
    monkeypatch,
):
    fixture = single_connection_radar
    calls = {"ipc": 0, "fallback": 0, "projection": 0}

    async def current(*_args, **_kwargs):
        calls["ipc"] += 1
        assert fixture.engine.pool.checkedout() == 0
        # This actually borrows the only pool connection during mocked collector
        # IPC. An unclosed list/auth transaction would make this impossible.
        async with fixture.engine.connect() as probe:
            assert await probe.scalar(text("SELECT 1")) == 1
        return {}, None

    async def fallback(_request, db):
        calls["fallback"] += 1
        assert fixture.engine.pool.checkedout() == 0
        await db.scalar(text("SELECT 1"))
        assert db.in_transaction() and fixture.engine.pool.checkedout() == 1
        return synthetic_health()

    original_project = api.project

    def project(*args, **kwargs):
        calls["projection"] += 1
        assert fixture.engine.pool.checkedout() == 0
        return original_project(*args, **kwargs)

    monkeypatch.setattr(api, "collector_current", current)
    monkeypatch.setattr(api, "collector_diagnostics", fallback)
    monkeypatch.setattr(api, "project", project)
    response = await fixture.client.get("/api/v1/opportunities?asset=SYNTH_API&history=true")
    assert response.status_code == 200
    assert calls["ipc"] == calls["fallback"] == 1
    assert calls["projection"] >= 3
    assert response.json()["items"][0]["net_profit_cad"] == "20"
    assert fixture.engine.pool.checkedout() == 0




