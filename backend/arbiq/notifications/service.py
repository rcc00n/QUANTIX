"""Authenticated pairing, durable outbox and auditable owner controls."""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update

from arbiq.auth.security import hash_token
from arbiq.db.models import User
from arbiq.notifications.models import (
    AlertDecision,
    AlertLifecycle,
    NotificationEvent,
    NotificationOutbox,
    NotificationWorkerState,
    TelegramBinding,
    TelegramChallenge,
)
from arbiq.settings.terminal import read_effective_terminal


def audit(db, event, *, user_id=None, outbox_id=None, alert_id=None, details=None, key=None):
    db.add(
        NotificationEvent(
            user_id=user_id,
            outbox_id=outbox_id,
            alert_id=alert_id,
            event_type=event,
            details=details or {},
            idempotency_key=key,
        )
    )


async def binding_for(db, user_id):
    return await db.scalar(
        select(TelegramBinding).where(
            TelegramBinding.user_id == user_id, TelegramBinding.revoked_at.is_(None)
        )
    )


async def delivery_status(db, user_id, now=None):
    now = now or datetime.now(UTC)
    binding = await binding_for(db, user_id)
    state = await db.get(NotificationWorkerState, "telegram")
    _, config = await read_effective_terminal(db, user_id)
    worker_recent = bool(state and 0 <= (now - state.heartbeat_at).total_seconds() <= 90)
    health = bool(worker_recent and state.healthy)
    pending = list(
        await db.scalars(
            select(TelegramChallenge)
            .where(
                TelegramChallenge.user_id == user_id,
                TelegramChallenge.expires_at > now,
                TelegramChallenge.consumed_at.is_(None),
            )
            .order_by(TelegramChallenge.created_at.desc())
            .limit(5)
        )
    )
    counts = dict(
        (
            await db.execute(
                select(NotificationOutbox.status, func.count())
                .where(NotificationOutbox.user_id == user_id)
                .group_by(NotificationOutbox.status)
            )
        ).all()
    )
    return {
        "paired": binding is not None,
        "pairing": {
            "id": str(binding.id),
            "username": binding.telegram_username,
            "approved_at": binding.approved_at.isoformat(),
            "paused": binding.paused,
        }
        if binding
        else None,
        "pending_challenges": [
            {
                "id": str(x.id),
                "expires_at": x.expires_at.isoformat(),
                "requested": x.requested_at is not None,
                "telegram_username": x.telegram_username,
                "requested_at": x.requested_at.isoformat() if x.requested_at else None,
            }
            for x in pending
        ],
        "worker": {
            "heartbeat_at": state.heartbeat_at.isoformat(),
            "healthy": health,
            **state.details,
        }
        if state
        else None,
        "telegram_enabled": config.notifications.telegram_enabled,
        "delivery_active": bool(
            binding and not binding.paused and health and config.notifications.telegram_enabled
        ),
        "providers": {
            "telegram": {"available": health},
            "push": {"enabled": False, "reason": "PAID_PROVIDER_NOT_AUTHORIZED"},
            "voice": {"enabled": False, "reason": "PAID_PROVIDER_NOT_AUTHORIZED"},
        },
        "queue_counts": counts,
        "delivery_semantics": "Durable retries for explicit rejection; uncertain sends "
        "require review",
    }


async def create_challenge(db, user_id, now):
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if await binding_for(db, user_id):
        raise ValueError("ALREADY_PAIRED")
    # One current enrollment per authenticated owner. Invalidated challenges cannot replay.
    await db.execute(
        update(TelegramChallenge)
        .where(TelegramChallenge.user_id == user_id, TelegramChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    token = secrets.token_urlsafe(24)
    challenge = TelegramChallenge(
        user_id=user_id, token_hash=hash_token(token), expires_at=now + timedelta(minutes=10)
    )
    db.add(challenge)
    await db.flush()
    audit(db, "pairing.challenge_created", user_id=user_id)
    state = await db.get(NotificationWorkerState, "telegram")
    username = state.details.get("bot_username") if state else None
    return {
        "challenge_id": str(challenge.id),
        "challenge": token,
        "start_url": f"https://t.me/{username}?start={token}" if username else None,
        "expires_at": challenge.expires_at.isoformat(),
    }


async def request_pairing(db, token, chat, sender, now):
    if chat.get("type") != "private" or chat.get("id") != sender.get("id"):
        return False
    row = await db.scalar(
        select(TelegramChallenge)
        .where(
            TelegramChallenge.token_hash == hash_token(token),
            TelegramChallenge.expires_at > now,
            TelegramChallenge.consumed_at.is_(None),
        )
        .with_for_update()
    )
    if not row or row.requested_at is not None:
        return False
    existing = await db.scalar(
        select(TelegramBinding).where(
            (TelegramBinding.chat_id == chat["id"])
            | (TelegramBinding.telegram_user_id == sender["id"])
        )
    )
    if existing and existing.revoked_at is None:
        return False
    row.chat_id, row.telegram_user_id = chat["id"], sender["id"]
    row.telegram_username = str(sender["username"])[:100] if sender.get("username") else None
    row.requested_at = now
    audit(db, "pairing.approval_requested", user_id=row.user_id)
    return True


async def approve_pairing(db, user_id, challenge_id, now):
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    challenge = await db.scalar(
        select(TelegramChallenge)
        .where(
            TelegramChallenge.id == challenge_id,
            TelegramChallenge.user_id == user_id,
            TelegramChallenge.expires_at > now,
            TelegramChallenge.consumed_at.is_(None),
            TelegramChallenge.requested_at.is_not(None),
        )
        .with_for_update()
    )
    if not challenge or challenge.chat_id is None:
        raise ValueError("PAIRING_CHALLENGE_UNAVAILABLE")
    row = await db.scalar(select(TelegramBinding).where(TelegramBinding.user_id == user_id))
    other = await db.scalar(
        select(TelegramBinding).where(
            TelegramBinding.user_id != user_id,
            (TelegramBinding.chat_id == challenge.chat_id)
            | (TelegramBinding.telegram_user_id == challenge.telegram_user_id),
        )
    )
    if other:
        raise ValueError("TELEGRAM_ACCOUNT_ALREADY_BOUND")
    if row is not None and row.revoked_at is None:
        raise ValueError("ALREADY_PAIRED")
    if row is None:
        row = TelegramBinding(user_id=user_id)
        db.add(row)
    row.chat_id, row.telegram_user_id = challenge.chat_id, challenge.telegram_user_id
    row.telegram_username, row.approved_at = challenge.telegram_username, now
    row.paused, row.revoked_at, challenge.consumed_at = False, None, now
    audit(db, "pairing.approved", user_id=user_id)
    await db.flush()
    return row


async def revoke_pairing(db, user_id, now):
    binding = await binding_for(db, user_id)
    if binding:
        binding.revoked_at = now
        await db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.user_id == user_id,
                NotificationOutbox.status.in_(("pending", "retry")),
            )
            .values(status="cancelled", error_code="PAIRING_REVOKED")
        )
        audit(db, "pairing.revoked", user_id=user_id)
    await db.execute(
        update(TelegramChallenge)
        .where(TelegramChallenge.user_id == user_id, TelegramChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
    )


