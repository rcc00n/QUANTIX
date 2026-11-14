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










