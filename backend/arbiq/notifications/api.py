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


