"""All users, books, amounts and venues in these API fixtures are SYNTHETIC."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arbiq.auth.security import hash_password
from arbiq.core.config import Settings
from arbiq.db.models import (
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    Asset,
    CollectorRun,
    Exchange,
    Market,
    SystemEvent,
)
from arbiq.main import create_app
from arbiq.opportunities import api
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def synthetic_payload(now=NOW, profit="10", **changes):
    return {
        "base_asset": "SYNTH_API",
        "buy_exchange": "synthetic_api_buy",
        "sell_exchange": "synthetic_api_sell",
        "buy_symbol": "SYNTH/CAD",
        "sell_symbol": "SYNTH/CAD",
        "status": "VERIFIED_MARKET",
        "actionable": False,
        "eligibility": "UNVERIFIED",
        "calculation_version": "synthetic-test-only",
        "selected_budget_cad": "1000",
        "quantity": "1",
        "buy_price": "100",
        "sell_price": "110",
        "acquisition_cost_cad": "100",
        "net_profit_cad": profit,
        "net_return_pct": "10",
        "detected_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "supporting_received_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "reasons": ["SYNTHETIC_UNVERIFIED_ELIGIBILITY"],
        **changes,
    }


def synthetic_health(now=NOW, trusted=True, host=True, healthy=True):
    return {
        "healthy": healthy,
        "clock_healthy": trusted and host,
        "clock": {"utc_now": now.isoformat(), "trusted": trusted, "host_healthy": host},
    }


def synthetic_route(payload=None):
    return SimpleNamespace(
        id=uuid4(),
        status="VERIFIED_MARKET",
        detected_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        payload=payload or synthetic_payload(),
    )


def test_retained_clock_gates_expiry_and_exact_decimal_sort():
    route = synthetic_route()
    assert (
        api.project(route, Decimal(1000), synthetic_health(), 30)["status"]
        == "VERIFIED_MARKET"
    )
    for health in (
        synthetic_health(trusted=False),
        synthetic_health(host=False),
        synthetic_health(healthy=False),
    ):
        result = api.project(route, Decimal(1000), health, 30)
        assert result["status"] == "RESEARCH_ONLY"
        assert result["actionable"] is False
    assert (
        api.project(
            route, Decimal(1000), synthetic_health(NOW + timedelta(seconds=31)), 30
        )["status"]
        == "EXPIRED"
    )
    # The two values differ after Decimal's default 28 significant digits.
    rows = [
        {"id": "low", "net_profit_cad": "10000000000.000000000000000001"},
        {"id": "high", "net_profit_cad": "10000000000.000000000000000002"},
        {"id": "missing", "net_profit_cad": None},
    ]
    assert [
        x["id"]
        for x in sorted(rows, key=lambda x: api.financial_sort(x, "net_profit_cad"))
    ] == ["high", "low", "missing"]






async def seed_routes(database_url):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            suffix = uuid4().hex
            base = Asset(
                canonical_key=f"synthetic-api:{suffix}", symbol="SYNTH", kind="crypto"
            )
            quote = Asset(
                canonical_key=f"synthetic-cad:{suffix}", symbol="CAD", kind="fiat"
            )
            buy = Exchange(slug=f"synthetic-api-buy-{suffix}", name="SYNTHETIC buy")
            sell = Exchange(slug=f"synthetic-api-sell-{suffix}", name="SYNTHETIC sell")
            run = CollectorRun(
                worker_id=f"synthetic-api-{suffix}",
                status="stopped",
                heartbeat_at=NOW,
                config_snapshot={},
                metrics={},
            )
            db.add_all([base, quote, buy, sell, run])
            await db.flush()
            markets = [
                Market(
                    exchange_id=venue.id,
                    base_asset_id=base.id,
                    quote_asset_id=quote.id,
                    native_symbol="SYNTH/CAD",
                )
                for venue in (buy, sell)
            ]
            db.add_all(markets)
            await db.flush()
            ids = []
            for index, profit in enumerate(("10", "20", None)):
                payload = synthetic_payload(profit=profit)
                route = ArbitrageRoute(
                    route_key=f"synthetic-api-{suffix}-{index}",
                    buy_market_id=markets[0].id,
                    sell_market_id=markets[1].id,
                    run_id=run.id,
                    status="VERIFIED_MARKET",
                    actionable=False,
                    detected_at=NOW,
                    updated_at=NOW,
                    expires_at=NOW + timedelta(seconds=60),
                    revision=0,
                    payload=payload,
                )
                db.add(route)
                await db.flush()
                ids.append(str(route.id))
                db.add(
                    ArbitrageLifecycleEvent(
                        route_id=route.id,
                        event_at=NOW,
                        status="DETECTED",
                        reason="SYNTHETIC API fixture",
                        details={},
                    )
                )
            await db.commit()
            return ids
    finally:
        await engine.dispose()


@pytest.fixture
def radar_client(database_url):
    username = f"synthetic-radar-{uuid4().hex[:16]}"
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        admin_username=username,
        admin_password_hash=hash_password("synthetic-radar-password"),
        allowed_origins=["http://testserver"],
    )
    with TestClient(
        create_app(settings), client=(f"synthetic-{uuid4().hex}", 12345)
    ) as client:
        yield client, username


def login(client, username):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "synthetic-radar-password"},
    )
    assert response.status_code == 200




