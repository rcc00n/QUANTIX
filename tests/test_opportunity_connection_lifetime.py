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


@pytest.mark.parametrize("history", [False, True])
async def test_scalar_candidate_path_matches_full_projection_and_loads_only_needed_payloads(
    single_connection_radar,
    monkeypatch,
    history,
):
    fixture = single_connection_radar
    live_id, retained_id, terminal_id = fixture.seeded_ids
    ids = {UUID(item) for item in fixture.seeded_ids}
    asset = f"SYNTH8B1_{uuid4().hex[:10].upper()}"
    health = synthetic_health(NOW + timedelta(seconds=3))
    current = {
        live_id: synthetic_payload(
            NOW + timedelta(seconds=2),
            profit="11.000000000000000001",
            base_asset=asset,
        ),
        retained_id: synthetic_payload(
            profit="99", expires_at="SYNTHETIC invalid expiry", base_asset=asset
        ),
    }
    async with fixture.sessions() as db:
        for row in await db.scalars(select(ArbitrageRoute).where(ArbitrageRoute.id.in_(ids))):
            row.payload = {**row.payload, "base_asset": asset}
            if str(row.id) in {live_id, terminal_id}:
                row.status = "EXPIRED"
        await db.commit()
        original_rows = (
            await db.execute(
                api.list_route_query(Decimal(1000), full=True).where(ArbitrageRoute.id.in_(ids))
            )
        ).all()

    expected = []
    for row in original_rows:
        projected = api.project(row, Decimal(1000), health, 30, current=current.get(str(row.id)))
        if history or projected["status"] not in api.TERMINAL:
            expected.append({k: v for k, v in projected.items() if k not in api.LIST_DETAIL_FIELDS})
    expected.sort(key=lambda row: api.financial_sort(row, "net_profit_cad"))

    async def collector(*_args, **_kwargs):
        assert fixture.engine.pool.checkedout() == 0
        return current, health

    monkeypatch.setattr(api, "collector_current", collector)
    fixture.route_queries.clear()
    fixture.route_parameters.clear()
    response = await fixture.client.get(
        f"/api/v1/opportunities?asset={asset}&history={str(history).lower()}"
    )
    assert response.status_code == 200
    assert response.json()["items"] == expected
    assert expected[0]["id"] == retained_id and expected[0]["net_profit_cad"] == "20"
    assert (
        expected[1]["id"] == live_id and expected[1]["storage_origin"] == "live_engine_projection"
    )
    assert len(fixture.route_queries) == 3
    assert "payload" not in fixture.route_queries[1].split("FROM")[0]
    payload_ids = {str(value) for value in fixture.route_parameters[2]} & set(fixture.seeded_ids)
    assert payload_ids == ({retained_id, terminal_id} if history else {retained_id})
    assert f'desc="{len(payload_ids)} rows"' in response.headers["Server-Timing"]
    assert fixture.engine.pool.checkedout() == 0


async def test_busy_custom_gate_rejects_duplicate_before_any_route_sql_or_payload_load(
    single_connection_radar,
    monkeypatch,
):
    fixture = single_connection_radar
    entered, release = asyncio.Event(), asyncio.Event()

    async def current(*_args, **_kwargs):
        assert fixture.engine.pool.checkedout() == 0
        entered.set()
        await release.wait()
        return {}, synthetic_health()

    monkeypatch.setattr(api, "collector_current", current)
    path = "/api/v1/opportunities?asset=SYNTH_API&history=true&budget_cad=250"
    first = asyncio.create_task(fixture.client.get(path))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        loaded_before_duplicate = len(fixture.route_queries)
        assert loaded_before_duplicate == 2  # Count and scalar candidate window.
        assert "payload" not in fixture.route_queries[1].split("FROM")[0]
        duplicate = await fixture.client.get(path)
        assert duplicate.status_code == 429
        assert duplicate.headers["retry-after"] == "2"
        assert len(fixture.route_queries) == loaded_before_duplicate
    finally:
        release.set()
        result = await first
    assert result.status_code == 200
    assert fixture.engine.pool.checkedout() == 0
