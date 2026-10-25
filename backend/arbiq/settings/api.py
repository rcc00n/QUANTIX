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


class UpdateTerminal(Contract):
    settings: TerminalSettings
    expected_version: int | None = Field(default=None, ge=0)


@router.get("/terminal")
async def get_terminal(request: Request, db: DB, user: AuthenticatedUser):
    row, settings = await read_effective_terminal(db, user.id)
    now, _ = await utc_reference(request)
    return settings_dto(row, settings, await delivery_status(db, user.id, now))


@router.put("/terminal")
async def update_terminal(body: UpdateTerminal, request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    known = set(await db.scalars(select(Exchange.slug).where(Exchange.enabled.is_(True))))
    if set(body.settings.market.enabled_exchanges) - known:
        raise HTTPException(422, "Market preferences reference an unavailable exchange adapter")
    requested_assets = set(body.settings.market.enabled_assets)
    if requested_assets:
        assets = list(
            await db.scalars(
                select(Asset).where(
                    Asset.symbol.in_(requested_assets) | Asset.canonical_key.in_(requested_assets)
                )
            )
        )
        known_assets = {v for asset in assets for v in (asset.symbol, asset.canonical_key)}
        if requested_assets - known_assets:
            raise HTTPException(422, "Market preferences reference an undiscovered asset")
    try:
        row = await save_terminal(db, user.id, body.settings, now, body.expected_version)
    except ValueError:
        raise HTTPException(
            409, "Settings changed; reload the applied version before saving"
        ) from None
    db.add(
        SystemEvent(
            actor_user_id=user.id,
            event_type="terminal.settings_changed",
            severity="info",
            message="Versioned terminal preferences saved",
            details={
                "version": row.version,
                "telegram_enabled": body.settings.notifications.telegram_enabled,
            },
        )
    )
    await db.commit()
    return settings_dto(row, body.settings, await delivery_status(db, user.id, now))


