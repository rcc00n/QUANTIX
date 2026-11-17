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
from arbiq.analytics.strategy_lab import StrategyLabRequest, strategy_cohort
from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import ReportExport, SystemEvent
from arbiq.market_data.api import utc_reference
from arbiq.paper_trading.api import now_checked

router = APIRouter(tags=["analytics and reports"])
_export_gate = asyncio.Semaphore(1)
_strategy_lab_gate = asyncio.Lock()


@router.post("/analytics/strategy-lab/compare")
async def compare_strategies(
    body: StrategyLabRequest, request: Request, db: DB, user: AuthenticatedUser
):
    if _strategy_lab_gate.locked():
        raise HTTPException(
            429, "A strategy comparison is already running", headers={"Retry-After": "5"}
        )
    async with _strategy_lab_gate:
        now = await now_checked(request)
        try:
            async with asyncio.timeout(8):
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                return await strategy_cohort(db, user.id, body, now)
        except TimeoutError:
            raise HTTPException(
                503, "Comparison exceeded its query budget. Select a shorter period."
            ) from None


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


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@router.get("/reports/export")
async def report_export(
    session_id: UUID,
    request: Request,
    db: DB,
    user: AuthenticatedUser,
    start: datetime | None = None,
    end: datetime | None = None,
    strategy_version_id: UUID | None = None,
):
    # Authentication precedes all disk/work. A single job prevents export fanout from
    # exhausting the modest VPS; the frontend receives a useful retry response.
    owner_id = user.id
    await owned_session(db, owner_id, session_id, strategy_version_id)
    # Release the read-only authentication transaction before a long export. The
    # repeatable snapshot needs only one pool slot; ordinary dashboard reads keep
    # the other slots. Capture the UUID first because rollback expires ORM users.
    await db.rollback()
    if _export_gate.locked():
        raise HTTPException(
            429,
            "Another analytical export is running; retry shortly",
            headers={"Retry-After": "10"},
        )
    async with _export_gate:
        now, clock = await utc_reference(request)
        path = None
        try:
            # A separate snapshot starts before ANY query; the auth dependency may
            # already have queried on its own connection. Export is read-only.
            async with request.app.state.sessions() as snapshot:
                await snapshot.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                session, control = await owned_session(
                    snapshot, owner_id, session_id, strategy_version_id
                )
                period = period_for(session, start, end, now)
                try:
                    async with asyncio.timeout(MAX_GENERATION_SECONDS):
                        path, manifest = await make_export(
                            snapshot, session, control, period, now, clock, strategy_version_id
                        )
                except TimeoutError:
                    raise HTTPException(
                        413, "Export exceeded90seconds; select a shorter period"
                    ) from None
            digest = await asyncio.to_thread(file_hash, path)
            # Audit separately after the read-only snapshot. No credentials/config
            # dumps enter these records. A failed audit prevents serving the file.
            event = ReportExport(
                user_id=owner_id,
                schema_version="quantix-export-v1",
                period_start=period.start,
                period_end=period.end,
                status="completed",
                content_sha256=digest,
                provenance={
                    "session_id": str(session_id),
                    "classification": control.classification,
                    "strategy_version_id": str(strategy_version_id)
                    if strategy_version_id
                    else None,
                    "files": len(manifest["files"]),
                    "bytes": path.stat().st_size,
                    "generated_at": now.isoformat(),
                    "clock_trusted": clock.get("trusted", False),
                },
            )
            db.add(event)
            db.add(
                SystemEvent(
                    actor_user_id=owner_id,
                    event_type="report.export_completed",
                    severity="info",
                    message="Authenticated paper analytical archive generated",
                    details={
                        "session_id": str(session_id),
                        "content_sha256": digest,
                        "paper_only": True,
                    },
                )
            )
            await db.commit()
            return FileResponse(
                path,
                media_type="application/zip",
                filename=f"quantix-analysis-{session_id}-{period.end:%Y%m%dT%H%M%SZ}.zip",
                headers={"X-Content-SHA256": digest, "Cache-Control": "no-store"},
                background=BackgroundTask(path.unlink, missing_ok=True),
            )
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
