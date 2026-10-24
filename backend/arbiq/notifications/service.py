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


