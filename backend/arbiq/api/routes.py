import asyncio

import aiohttp
from fastapi import APIRouter, Request
from sqlalchemy import func, select, text

from arbiq.analytics.contracts import (
    CSV_COLUMNS,
    EXPORT_FILES,
    AnalyticsExportManifest,
    AnalyticsExportMetrics,
    DailySummary,
)
from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import ArbitrageRoute, Exchange, PaperSession, SystemEvent
from arbiq.market_data.api import collector_state
from arbiq.notifications.service import delivery_status
from arbiq.opportunities.api import collector_diagnostics
from arbiq.settings.contracts import StrategySettings

router = APIRouter()


@router.get("/system/status")
async def system_status(request: Request, db: DB, user: AuthenticatedUser) -> dict:
    _, collector_healthy = await collector_state(db)
    await db.commit()  # Read-only heartbeat lookup must not hold a slot during IPC.

    async def paper_health():
        if request.app.state.collector_http is not None:
            try:
                async with request.app.state.collector_http.get("/paper/status") as response:
                    if response.status == 200:
                        return (await response.json()).get("healthy", False)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                pass
        return False

    diagnostics, paper_healthy = await asyncio.gather(
        collector_diagnostics(request, db), paper_health()
    )
    revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
    notification = await delivery_status(db, user.id)
    return {
        "mode": "paper",
        "stage": 7,
        "database": "connected",
        "schema_revision": revision,
        "services": {
            "scanner": "running" if diagnostics.get("healthy") else "unavailable",
            "market_data": "running"
            if diagnostics.get("healthy") or collector_healthy
            else "unavailable",
            "simulator": "running" if paper_healthy else "unavailable",
            "telegram": "active" if notification["delivery_active"] else "unpaired_or_inactive",
            "ai": "configured" if request.app.state.settings.ai_enabled else "disabled",
            "reporting": "ledger_analytics_and_authenticated_zip_export",
        },
        "counts": {
            "exchanges": await db.scalar(select(func.count()).select_from(Exchange)),
            "opportunities": await db.scalar(select(func.count()).select_from(ArbitrageRoute)),
            "paper_sessions": await db.scalar(select(func.count()).select_from(PaperSession)),
        },
    }


@router.get("/system/events")
async def events(db: DB, user: AuthenticatedUser) -> list[dict]:
    rows = (
        await db.scalars(
            select(SystemEvent).order_by(SystemEvent.created_at.desc(), SystemEvent.id).limit(30)
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "created_at": row.created_at,
            "event_type": row.event_type,
            "severity": row.severity,
            "message": row.message,
        }
        for row in rows
    ]


@router.get("/settings/defaults")
async def settings_defaults(user: AuthenticatedUser) -> dict:
    return {
        "status": "defaults_only",
        "persisted": False,
        "settings": StrategySettings().model_dump(mode="json"),
    }


@router.get("/reports/contract")
async def report_contract(user: AuthenticatedUser) -> dict:
    return {
        "schema_version": "1.0",
        "status": "implemented",
        "export_available": True,
        "files": EXPORT_FILES,
        "csv_columns": CSV_COLUMNS,
        "metrics_schema": AnalyticsExportMetrics.model_json_schema(),
        "manifest_schema": AnalyticsExportManifest.model_json_schema(),
        "legacy_daily_summary_schema": DailySummary.model_json_schema(),
    }
