"""Independent singleton Telegram worker. Run with python -m arbiq.notifications.worker."""

import asyncio
import contextlib
import json
import signal
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import structlog
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from arbiq.core.clock import TrustedClock, elapsed_seconds
from arbiq.core.config import get_settings
from arbiq.core.logging import configure_logging
from arbiq.db.schema import EXPECTED_SCHEMA_REVISION
from arbiq.notifications.commands import handle_update
from arbiq.notifications.models import (
    AlertDecision,
    AlertLifecycle,
    NotificationEvent,
    NotificationOutbox,
    NotificationWorkerState,
    TelegramBinding,
)
from arbiq.notifications.reports import schedule_reports
from arbiq.notifications.service import audit
from arbiq.notifications.telegram import TelegramClient, TelegramError, read_bot_token
from arbiq.settings.terminal import read_effective_terminal
from arbiq.signals.engine import revalidate_delivery, scan, schedule_repeats

log = structlog.get_logger()
LOCK_KEY = 7271142606
HEALTH_PATH = Path("/tmp/quantix-notification-health.json")


def retry_delay(attempt, retry_after=None):
    return max(min(2 ** min(attempt, 10), 900), min(retry_after or 0, 86400))


def failure_state(error, attempt):
    if error.ambiguous:
        return "ambiguous"
    if (
        error.code == "TELEGRAM_429"
        or error.code == "TELEGRAM_NETWORK"
        or error.code.startswith("TELEGRAM_5")
    ):
        return "retry" if attempt < 8 else "failed"
    return "failed"


async def recover_sending(db):
    rows = list(
        await db.scalars(
            select(NotificationOutbox).where(NotificationOutbox.status == "sending").limit(1000)
        )
    )
    for row in rows:
        row.status, row.error_code = "ambiguous", "RESTART_DURING_SEND"
        audit(
            db,
            "delivery.ambiguous",
            user_id=row.user_id,
            outbox_id=row.id,
            alert_id=row.alert_id,
            details={"reason": "RESTART_DURING_SEND"},
        )


class NotificationWorker:
    def __init__(self, settings, sessions, client, clock, singleton=None):
        self.settings, self.sessions, self.client, self.clock = settings, sessions, client, clock
        self.singleton = singleton
        self.stop = asyncio.Event()
        self.details = {}
        self.healthy = False
        self.last_send = float("-inf")

    @contextlib.asynccontextmanager
    async def session(self):
        async with self.sessions() as db:
            db.info["notification_clock"] = self.clock
            yield db

    async def heartbeat(self):
        now = self.clock.utcnow()
        async with self.session() as db:
            state = await db.get(NotificationWorkerState, "telegram")
            if state is None:
                state = NotificationWorkerState(key="telegram", update_offset=0)
                db.add(state)
            state.heartbeat_at, state.healthy = now, self.healthy
            state.details = {**self.details, "clock_trusted": self.clock.snapshot()["trusted"]}
            await db.commit()
        await asyncio.to_thread(
            HEALTH_PATH.write_text,
            json.dumps(
                {
                    "heartbeat_monotonic": elapsed_seconds(),
                    "healthy": self.healthy,
                    "error_code": self.details.get("error_code"),
                }
            ),
        )
        await asyncio.to_thread(HEALTH_PATH.chmod, 0o600)

