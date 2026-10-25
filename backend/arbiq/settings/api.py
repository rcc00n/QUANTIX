"""Authenticated, versioned operator preferences with actual delivery availability."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from sqlalchemy import select

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import Asset, Exchange, SystemEvent
from arbiq.finance.contracts import Contract
from arbiq.market_data.api import utc_reference
from arbiq.notifications.service import delivery_status
from arbiq.paper_trading.api import now_checked
from arbiq.settings.models import TerminalSettingsVersion
from arbiq.settings.terminal import TerminalSettings, read_effective_terminal, save_terminal

router = APIRouter(prefix="/settings", tags=["persistent terminal settings"])


def settings_dto(row, settings, delivery=None):
    return {
        "version": row.version if row else 0,
        "id": str(row.id) if row else None,
        "effective_at": row.effective_at.isoformat() if row else None,
        "settings": settings.model_dump(mode="json"),
        "delivery_active": bool(delivery and delivery["delivery_active"]),
        "delivery_stage": 6,
        "market_application": "Collector reloads operator market preferences every60seconds",
        "defaults": TerminalSettings().model_dump(mode="json"),
    }


