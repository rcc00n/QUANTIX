"""Authenticated owner notification controls. Token and raw destination IDs never leave API."""

from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import ArbitrageEvaluation
from arbiq.finance.contracts import Contract
from arbiq.market_data.api import utc_reference
from arbiq.notifications.messages import alert_message
from arbiq.notifications.models import AlertDecision, NotificationEvent, NotificationOutbox
from arbiq.notifications.service import (
    acknowledge,
    approve_pairing,
    binding_for,
    create_challenge,
    delivery_status,
    enqueue,
    revoke_pairing,
    set_paused,
)
from arbiq.paper_trading.api import now_checked
from arbiq.settings.terminal import read_effective_terminal
from arbiq.signals.engine import evaluate_source

router = APIRouter(prefix="/notifications", tags=["owner Telegram notifications"])


class ApprovePairing(Contract):
    challenge_id: UUID


async def status(db, user, now=None):
    return await delivery_status(db, user.id, now)


@router.get("/status")
async def get_status(request: Request, db: DB, user: AuthenticatedUser):
    now, _ = await utc_reference(request)
    return await status(db, user, now)


@router.post("/pairing/challenge")
async def pairing_challenge(request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    try:
        result = await create_challenge(db, user.id, now)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await db.commit()
    return result


@router.post("/pairing/approve")
async def pairing_approve(body: ApprovePairing, request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    try:
        await approve_pairing(db, user.id, body.challenge_id, now)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await db.commit()
    return await status(db, user, now)


@router.post("/pairing/revoke")
async def pairing_revoke(request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    await revoke_pairing(db, user.id, now)
    await db.commit()
    return await status(db, user, now)


@router.post("/pause")
async def pause(request: Request, db: DB, user: AuthenticatedUser):
    try:
        await set_paused(db, user.id, True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await db.commit()
    return await status(db, user)


@router.post("/resume")
async def resume(request: Request, db: DB, user: AuthenticatedUser):
    try:
        await set_paused(db, user.id, False)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await db.commit()
    return await status(db, user)


@router.get("/history")
async def history(db: DB, user: AuthenticatedUser, limit: int = Query(default=30, ge=1, le=100)):
    rows = list(
        await db.scalars(
            select(NotificationOutbox)
            .where(NotificationOutbox.user_id == user.id)
            .order_by(NotificationOutbox.created_at.desc())
            .limit(limit)
        )
    )
    return {
        "items": [
            {
                "id": str(x.id),
                "kind": x.kind,
                "status": x.status,
                "created_at": x.created_at,
                "expires_at": x.expires_at,
                "delivered_at": x.delivered_at,
                "error_code": x.error_code,
                "attempts": x.attempts,
                "message": x.message,
                "alert_id": str(x.alert_id) if x.alert_id else None,
            }
            for x in rows
        ],
        "limit": limit,
    }


@router.get("/preview")
async def preview(request: Request, db: DB, user: AuthenticatedUser):
    # Preview is immutable retained evidence, never a send or a fresh executable promise.
    now, clock = await utc_reference(request)
    source = await db.scalar(
        select(ArbitrageEvaluation).order_by(ArbitrageEvaluation.evaluated_at.desc()).limit(1)
    )
    if source is None:
        return {
            "message": None,
            "eligible": False,
            "reasons": ["NO_EVALUATION"],
            "classification": None,
            "evaluation_id": None,
        }
    _, terminal = await read_effective_terminal(db, user.id)
    gate = await evaluate_source(db, source, terminal.notifications, now, clock["trusted"])
    return {
        "message": alert_message(
            source.result, now, request.app.state.settings.public_app_url, emergency=gate.emergency
        ),
        "eligible": gate.eligible,
        "reasons": gate.reasons,
        "classification": gate.classification,
        "evaluation_id": str(source.id),
    }


@router.post("/test")
async def test_notification(request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    binding = await binding_for(db, user.id)
    if not binding:
        raise HTTPException(409, "TELEGRAM_NOT_PAIRED")
    try:
        row = await enqueue(
            db,
            binding,
            key=f"test:{user.id}:{uuid4()}",
            kind="test",
            now=now,
            expires_at=now + timedelta(minutes=5),
            message=(
                "QUANTIX — OWNER TEST\nTelegram delivery test. PAPER ONLY. No market opportunity, "
                "profit or execution is implied.\n" + now.isoformat()
            ),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await db.commit()
    return {"id": str(row.id), "status": row.status}


@router.post("/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: UUID, request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    try:
        await acknowledge(db, user.id, alert_id, now)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    await db.commit()
    return {"acknowledged": True}


@router.post("/{alert_id}/cancel")
async def cancel_alert(alert_id: UUID, request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    try:
        await acknowledge(db, user.id, alert_id, now, cancel=True)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    await db.commit()
    return {"cancelled": True}


