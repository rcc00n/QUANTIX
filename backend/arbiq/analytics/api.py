"""Authenticated historical analytics and bounded analytical archive generation."""

import asyncio
import hashlib
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import text
from starlette.background import BackgroundTask

from arbiq.analytics.export import MAX_GENERATION_SECONDS, make_export
from arbiq.analytics.service import analytics, owned_session, period_for, series
from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import ReportExport, SystemEvent
from arbiq.market_data.api import utc_reference

router = APIRouter(tags=["analytics and reports"])
_export_gate = asyncio.Semaphore(1)


@router.get("/analytics/sessions/{session_id}")
async def session_analytics(
    session_id: UUID,
    request: Request,
    db: DB,
    user: AuthenticatedUser,
    start: datetime | None = None,
    end: datetime | None = None,
    strategy_version_id: UUID | None = None,
):
    session, control = await owned_session(db, user.id, session_id, strategy_version_id)
    now, clock = await utc_reference(request)
    period = period_for(session, start, end, now)
    value = await analytics(db, session, control, period, strategy_version_id)
    value["clock"] = clock
    return value


@router.get("/analytics/sessions/{session_id}/series")
async def session_series(
    session_id: UUID,
    request: Request,
    db: DB,
    user: AuthenticatedUser,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=300, ge=2, le=500),
):
    session, control = await owned_session(db, user.id, session_id)
    now, clock = await utc_reference(request)
    period = period_for(session, start, end, now)
    return {
        **await series(db, session.id, period, limit),
        "session_id": str(session.id),
        "classification": control.classification,
        "period": period.model_dump(mode="json"),
        "clock": clock,
    }


