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


