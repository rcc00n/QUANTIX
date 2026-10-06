"""Authenticated read APIs over public market data; no trading endpoints."""

from datetime import UTC, datetime
from uuid import UUID

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import (
    CollectorRun,
    Exchange,
    ExchangeHealth,
    LatestMarketBook,
    Market,
    MarketQualityEvent,
)
from arbiq.market_data.quality import CapturedBook, DataState, fresh_view
from arbiq.market_data.timing import age_seconds, freshness

router = APIRouter(tags=["public market intelligence"])


async def collector_state(db, now=None):
    run = await db.scalar(select(CollectorRun).where(CollectorRun.status == "running").limit(1))
    if run is None:
        run = await db.scalar(
            select(CollectorRun).order_by(CollectorRun.created_at.desc()).limit(1)
        )
    healthy = bool(
        run
        and run.status == "running"
        and 0 <= ((now or datetime.now(UTC)) - run.heartbeat_at).total_seconds() < 30
    )
    return run, healthy


async def utc_reference(request):
    client = request.app.state.collector_http
    if client is not None:
        try:
            async with client.get("/clock") as response:
                if response.status == 200:
                    clock = await response.json()
                    if clock.get("utc_now"):
                        return datetime.fromisoformat(clock["utc_now"]), clock
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            pass
    return datetime.now(UTC), {"trusted": False, "host_healthy": False}


@router.get("/market-data/status")
async def status(request: Request, db: DB, user: AuthenticatedUser):
    now, clock = await utc_reference(request)
    run, healthy = await collector_state(db, now)
    return {
        "status": "running" if healthy else "unavailable",
        "collector_healthy": healthy,
        "heartbeat_at": run.heartbeat_at if run else None,
        "run_id": str(run.id) if run else None,
        "metrics": run.metrics if run else {},
        "mode": "paper",
        "arbitrage_engine": "depth_scanner",
        "source": "persisted_collector_heartbeat",
        "stage": 3,
    }


@router.get("/exchanges")
async def exchanges(request: Request, db: DB, user: AuthenticatedUser):
    live = {}
    ipc_failed = getattr(getattr(request, "state", None), "collector_unavailable", False)
    client = request.app.state.collector_http
    if client is not None and not ipc_failed:
        try:
            async with client.get("/feeds/status") as response:
                if response.status == 200:
                    live = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            ipc_failed = True
    clock = live.get("clock", {}) if isinstance(live, dict) else {}
    if clock.get("utc_now"):
        now = datetime.fromisoformat(clock["utc_now"])
    elif ipc_failed:
        # A second serial request to the same failed IPC cannot establish trust.
        now, clock = datetime.now(UTC), {"trusted": False, "host_healthy": False}
    else:
        now, clock = await utc_reference(request)
    run, healthy = await collector_state(db, now)
    live_current = bool(run and live.get("run_id") == str(run.id) and live.get("healthy"))
    rows = (
        await db.execute(
            select(Exchange, ExchangeHealth).outerjoin(ExchangeHealth).order_by(Exchange.slug)
        )
    ).all()
    result = []
    for exchange, health in rows:
        live_health = live.get("items", {}).get(exchange.slug, {}) if live_current else {}
        current = bool(healthy and health and health.run_id == run.id)
        state = health.state if current else "UNAVAILABLE"
        age = (
            age_seconds(now, health.last_success_at) if health and health.last_success_at else None
        )
        if current and health.last_success_at is not None and health.last_success_at > now:
            state = "SUSPICIOUS"
        if (
            current
            and age is not None
            and age > request.app.state.settings.collector.maximum_age_seconds
        ):
            state = "STALE"
        result.append(
            {
                "id": str(exchange.id),
                "slug": exchange.slug,
                "name": exchange.name,
                "enabled": exchange.enabled,
                "capabilities": exchange.capabilities,
                "state": state,
                "market_count": health.market_count if health else 0,
                "monitored_count": health.monitored_count if health else 0,
                "cad_market_count": health.cad_market_count if health else 0,
                "connection_mode": health.connection_mode if health else "not_connected",
                "latency_ms": health.latency_ms if health else None,
                "last_success_at": health.last_success_at if health else None,
                "last_attempt_at": health.last_attempt_at if health else None,
                "last_discovery_at": health.last_discovery_at if health else None,
                "successful_requests": health.successful_requests if health else 0,
                "failed_requests": health.failed_requests if health else 0,
                "reconnects": health.reconnects if health else 0,
                "error_code": health.error_code if health else "NOT_CONNECTED",
                "next_retry_at": health.next_retry_at if health else None,
                "public_data_verified": bool(health and health.last_success_at),
                "manual_trading_eligible": False,
                "status_generated_at": now.isoformat(),
                "last_status_check_at": health.updated_at if health else None,
                "last_collector_received_at": health.last_success_at if health else None,
                "last_source_update_at": None,
                "source": "persisted_status_snapshot",
                "clock_trusted": bool(clock.get("trusted")),
                "freshness_seconds": age if clock.get("trusted") else None,
                **live_health,
                "collector_healthy": live_current if live_health else current,
            }
        )
    return result


@router.get("/exchanges/{slug}/markets")
async def markets(
    slug: str,
    db: DB,
    user: AuthenticatedUser,
    search: str = Query(default="", max_length=80),
    monitored_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    exchange = await db.scalar(select(Exchange).where(Exchange.slug == slug))
    if exchange is None:
        raise HTTPException(404, "Exchange not found")
    query = select(Market).where(Market.exchange_id == exchange.id)
    if search:
        query = query.where(Market.native_symbol.ilike(f"%{search}%"))
    if monitored_only:
        query = query.where(
            Market.metadata_json["monitored"].as_boolean().is_(True), Market.active.is_(True)
        )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.scalars(query.order_by(Market.native_symbol).limit(limit).offset(offset))
    ).all()
    return {
        "exchange": slug,
        "total": total,
        "offset": offset,
        "limit": limit,
        "markets": [
            {
                "id": str(row.id),
                "symbol": row.native_symbol,
                "active": row.active,
                "base_asset_id": str(row.base_asset_id),
                "quote_asset_id": str(row.quote_asset_id),
                "amount_step": str(row.amount_step) if row.amount_step is not None else None,
                "price_tick": str(row.price_tick) if row.price_tick is not None else None,
                "min_notional": str(row.min_notional) if row.min_notional is not None else None,
                **row.metadata_json,
            }
            for row in rows
        ],
    }


