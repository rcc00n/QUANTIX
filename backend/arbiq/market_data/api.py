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


