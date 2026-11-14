"""Authenticated paper interfaces with SYNTHETIC isolated funds, never live executions."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from arbiq.db.models import Asset, Exchange, PaperSession, StrategyVersion
from arbiq.main import create_app
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.accounting import reconcile
from arbiq.settings.strategy import presets
from conftest import SYNTHETIC_PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def private_api(database_url, app_settings, monkeypatch):
    now = datetime(2026, 11, 22, 6, tzinfo=UTC)

    async def clock(request):
        return now

    monkeypatch.setattr("arbiq.paper_trading.api.now_checked", clock)
    suffix = uuid4().hex[:10]
    settings = app_settings.model_copy(update={"admin_username": "synthetic-paper-" + suffix})
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        for slug in ("kraken", "ndax"):
            if not await db.scalar(select(Exchange).where(Exchange.slug == slug)):
                db.add(Exchange(slug=slug, name="SYNTHETIC " + slug))
        if not await db.scalar(select(Asset).where(Asset.canonical_key == "fiat:CAD")):
            db.add(
                Asset(
                    canonical_key="fiat:CAD",
                    symbol="CAD",
                    kind="fiat",
                    identifiers={"synthetic": True},
                )
            )
        asset = Asset(
            canonical_key="synthetic-api:" + suffix,
            symbol="SYNTH",
            kind="crypto",
            identifiers={"synthetic": True},
        )
        db.add(asset)
        await db.commit()
        asset_key = asset.canonical_key
    with TestClient(create_app(settings), client=("synthetic-paper-api-" + suffix, 51000)) as c:
        assert (
            c.post(
                "/api/v1/auth/login",
                json={
                    "username": settings.admin_username,
                    "password": SYNTHETIC_PASSWORD,
                },
            ).status_code
            == 200
        )
        yield c, sessions, asset_key
    await engine.dispose()


def test_private_paper_endpoints_require_sessions(client):
    client.cookies.clear()
    for path in ("/paper/sessions", "/paper/trades", "/strategies"):
        assert client.get("/api/v1" + path).status_code == 401


async def test_funded_portfolios_and_mode_isolation(private_api):
    c, dbs, asset = private_api
    primary = c.post(
        "/api/v1/paper/sessions",
        json={"name": "SYNTHETIC validated", "starting_capital_cad": "10000"},
    )
    assert primary.status_code == 201, primary.text
    p = primary.json()
    assert p["classification"] == "VALIDATED"
    research = c.post(
        "/api/v1/paper/sessions",
        json={
            "name": "SYNTHETIC research",
            "starting_capital_cad": "10000",
            "classification": "RESEARCH",
            "allocations": [
                {
                    "exchange": "kraken",
                    "asset": asset,
                    "quantity": "50",
                    "cad_rate": "100",
                },
                {"exchange": "ndax", "quantity": "5000"},
            ],
        },
    )
    assert research.status_code == 201, research.text
    r = research.json()
    assert r["classification"] == "RESEARCH" and r["id"] != p["id"]
    listing = c.get("/api/v1/paper/sessions").json()
    assert listing["total"] == 2 and listing["scenarios_are_separate"]
    illegal = c.put(
        f"/api/v1/paper/sessions/{r['id']}/strategy",
        json=presets()["Balanced"].model_dump(mode="json"),
    )
    assert illegal.status_code == 409
    bad = c.post(
        "/api/v1/paper/sessions",
        json={
            "name": "SYNTHETIC freecrypto",
            "classification": "RESEARCH",
            "starting_capital_cad": "10000",
            "allocations": [
                {
                    "exchange": "kraken",
                    "asset": asset,
                    "quantity": "50",
                    "cad_rate": "100",
                }
            ],
        },
    )
    assert bad.status_code == 422
    async with dbs() as db:
        from uuid import UUID

        assert await reconcile(db, UUID(p["id"])) and await reconcile(db, UUID(r["id"]))


async def test_strategy_versioning_preserves_initial_configuration(private_api):
    c, dbs, _ = private_api
    p = c.post("/api/v1/paper/sessions", json={}).json()
    settings = p["settings"]
    settings["reaction_delay_seconds"] = 120
    settings["minimum_profit_cad"] = "12"
    response = c.put(f"/api/v1/paper/sessions/{p['id']}/strategy", json=settings)
    assert response.status_code == 200, response.text
    assert response.json()["strategy_version_id"] != p["strategy_version_id"]
    async with dbs() as db:
        from uuid import UUID

        session = await db.get(PaperSession, UUID(p["id"]))
        control = await db.get(PaperControl, session.id)
        old = await db.get(StrategyVersion, session.strategy_version_id)
        assert (
            str(old.id) == p["strategy_version_id"] and old.settings["reaction_delay_seconds"] == 30
        )
        assert control.settings["reaction_delay_seconds"] == 120
    settings["starting_capital_cad"] = "20000"
    assert c.put(f"/api/v1/paper/sessions/{p['id']}/strategy", json=settings).status_code == 409
    assert c.post(f"/api/v1/paper/sessions/{p['id']}/pause").json()["status"] == "PAUSED"
    assert c.post(f"/api/v1/paper/sessions/{p['id']}/resume").json()["status"] == "ACTIVE"






