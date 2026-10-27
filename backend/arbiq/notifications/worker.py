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

    async def dispatch_one(self):
        if elapsed_seconds() - self.last_send < 1.1:
            return False
        now = self.clock.utcnow()
        async with self.session() as db:
            row = await db.scalar(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status.in_(("pending", "retry")),
                    or_(
                        NotificationOutbox.available_at <= now, NotificationOutbox.expires_at <= now
                    ),
                )
                .order_by(NotificationOutbox.available_at, NotificationOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            binding = await db.get(TelegramBinding, row.binding_id)
            reason = None
            if row.expires_at <= now:
                row.status, reason = "expired", "MESSAGE_EXPIRED_BEFORE_DELIVERY"
            elif binding is None or binding.revoked_at is not None:
                row.status, reason = "cancelled", "PAIRING_REVOKED"
            elif row.kind not in ("command", "test", "completion"):
                _, terminal = await read_effective_terminal(db, row.user_id)
                if binding.paused or not terminal.notifications.telegram_enabled:
                    row.status, reason = "cancelled", "NOTIFICATIONS_PAUSED_OR_DISABLED"
            if reason is None:
                reason = await revalidate_delivery(db, row, now, self.clock.snapshot()["trusted"])
                if reason:
                    row.status = (
                        "expired"
                        if any(code in reason for code in ("EXPIRED", "CURRENT", "STALE"))
                        else "cancelled"
                    )
            if reason:
                row.error_code = reason
                audit(
                    db,
                    f"delivery.{row.status}",
                    user_id=row.user_id,
                    outbox_id=row.id,
                    alert_id=row.alert_id,
                    details={"reason": reason},
                )
                await db.commit()
                return True
            queued_details = await db.scalar(
                select(NotificationEvent.details)
                .where(
                    NotificationEvent.outbox_id == row.id,
                    NotificationEvent.event_type == "delivery.queued",
                )
                .order_by(NotificationEvent.created_at)
                .limit(1)
            )
            queued_at = (queued_details or {}).get("queue_insert_observed_at")
            row.attempts += 1
            row.status = "sending"
            audit(
                db,
                "delivery.sending",
                user_id=row.user_id,
                outbox_id=row.id,
                alert_id=row.alert_id,
                details={"attempt": row.attempts},
            )
            await db.commit()  # Durable before network. Crash here is explicitly ambiguous.
            row_id, chat_id, message, silent = row.id, binding.chat_id, row.message, row.silent
        gap = 1.1 - (elapsed_seconds() - self.last_send)
        if gap > 0:
            await asyncio.sleep(gap)
        self.last_send = elapsed_seconds()
        send_started = elapsed_seconds()
        send_started_at = self.clock.utcnow()
        try:
            result = await self.client.send(chat_id, message, silent=silent)
            failure = None
        except TelegramError as exc:
            result, failure = None, exc
        request_duration_ms = (elapsed_seconds() - send_started) * 1000
        provider_acknowledged_at = self.clock.utcnow()
        queue_ack_ms = None
        if queued_at and self.clock.snapshot().get("trusted"):
            try:
                queued_timestamp = datetime.fromisoformat(queued_at)
                if provider_acknowledged_at >= queued_timestamp:
                    queue_ack_ms = (
                        provider_acknowledged_at - queued_timestamp
                    ).total_seconds() * 1000
            except (TypeError, ValueError):
                pass  # Unavailable historical timing never becomes a zero-duration claim.
        async with self.session() as db:
            row = await db.get(NotificationOutbox, row_id)
            finished = self.clock.utcnow()
            if failure:
                row.status = failure_state(failure, row.attempts)
                row.error_code = failure.code
                row.available_at = finished + timedelta(
                    seconds=retry_delay(row.attempts, failure.retry_after)
                )
                audit(
                    db,
                    f"delivery.{row.status}",
                    user_id=row.user_id,
                    outbox_id=row.id,
                    alert_id=row.alert_id,
                    details={"code": failure.code, "attempt": row.attempts},
                )
                if failure.code == "TELEGRAM_429":
                    # Global worker backoff, not merely rescheduling one row.
                    self.last_send = elapsed_seconds() + retry_delay(
                        row.attempts, failure.retry_after
                    )
            else:
                row.status, row.error_code, row.delivered_at = "delivered", None, finished
                row.provider_message_id = str(result["message_id"])
                audit(
                    db,
                    "delivery.delivered",
                    user_id=row.user_id,
                    outbox_id=row.id,
                    alert_id=row.alert_id,
                    details={
                        "attempt": row.attempts,
                        "latency_ms": int((finished - row.created_at).total_seconds() * 1000)
                        if finished >= row.created_at and self.clock.snapshot().get("trusted")
                        else None,
                        "request_duration_ms": request_duration_ms,
                        "send_attempt_started_at": send_started_at.isoformat(),
                        "provider_acknowledged_at": provider_acknowledged_at.isoformat(),
                        "queue_to_provider_ack_ms": queue_ack_ms,
                        "queue_timestamp_basis": "enqueue_statement_observation_not_commit"
                        if queued_at
                        else "unavailable",
                        "acknowledgment_semantics": (
                            "Telegram accepted sendMessage; device receipt unverified"
                        ),
                    },
                )
                if row.alert_id:
                    decision = await db.get(AlertDecision, row.alert_id)
                    lifecycle = await db.scalar(
                        select(AlertLifecycle)
                        .where(AlertLifecycle.decision_id == row.alert_id)
                        .with_for_update()
                    )
                    _, terminal = await read_effective_terminal(db, row.user_id)
                    policy = terminal.notifications
                    if (
                        decision.emergency
                        and policy.repeat_until_acknowledged
                        and lifecycle
                        and not lifecycle.acknowledged_at
                        and not lifecycle.cancelled_at
                        and lifecycle.repeat_count < policy.maximum_repeats
                    ):
                        lifecycle.next_repeat_at = finished + timedelta(
                            seconds=policy.repeat_interval_seconds
                        )
            await db.commit()
        return True

    async def incoming_loop(self):
        while not self.stop.is_set():
            if not self.healthy:
                await asyncio.sleep(2)
                continue
            try:
                async with self.session() as db:
                    state = await db.get(NotificationWorkerState, "telegram")
                    offset = state.update_offset if state else 0
                updates = await self.client.updates(offset)
                for item in updates:
                    async with self.session() as db:
                        state = await db.get(
                            NotificationWorkerState, "telegram", with_for_update=True
                        )
                        if item["update_id"] < state.update_offset:
                            continue
                        await handle_update(
                            db,
                            item,
                            self.clock.utcnow(),
                            clock_trusted=self.clock.snapshot()["trusted"],
                            app_url=self.settings.public_app_url,
                        )
                        state.update_offset = item["update_id"] + 1
                        await (
                            db.commit()
                        )  # Command and offset commit together; no replay after restart.
            except TelegramError as exc:
                self.details["error_code"] = exc.code
                if exc.code in ("TELEGRAM_409", "TELEGRAM_401", "WEBHOOK_CONFLICT"):
                    self.healthy = False
                    self.details["consumer_conflict"] = exc.code == "TELEGRAM_409"
                    # 409 indicates another consumer: stop polling, require operator review/restart.
                    self.details["polling_blocked"] = True
                await asyncio.sleep(min(exc.retry_after or 10, 30))
            except Exception as exc:
                log.warning("notification_incoming_failed", error_type=type(exc).__name__)
                self.details["error_code"] = "INCOMING_DATABASE_OR_COMMAND_ERROR"
                await asyncio.sleep(5)

    async def run(self):
        async with self.session() as db:
            await recover_sending(db)
            await db.commit()
        incoming = asyncio.create_task(self.incoming_loop())
        next_ready, next_scan = 0.0, 0.0
        try:
            while not self.stop.is_set():
                if self.singleton:
                    connection, original_pid = self.singleton
                    if await connection.scalar(text("SELECT pg_backend_pid()")) != original_pid:
                        raise RuntimeError("Notification singleton connection changed")
                try:
                    elapsed = elapsed_seconds()
                    if elapsed >= next_ready:
                        if self.client and not self.details.get("polling_blocked"):
                            self.details.update(await self.client.readiness())
                            self.healthy = True
                            self.details["error_code"] = None
                            self.details["webhook_conflict"] = False
                        next_ready = elapsed + 60
                    if self.healthy and elapsed >= next_scan:
                        async with self.session() as db:
                            now = self.clock.utcnow()
                            trusted = self.clock.snapshot()["trusted"]
                            await scan(
                                db, now, clock_trusted=trusted, app_url=self.settings.public_app_url
                            )
                            await schedule_repeats(
                                db, now, clock_trusted=trusted, app_url=self.settings.public_app_url
                            )
                            if trusted:
                                bindings = list(
                                    await db.scalars(
                                        select(TelegramBinding)
                                        .where(TelegramBinding.revoked_at.is_(None))
                                        .limit(10)
                                    )
                                )
                                for binding in bindings:
                                    _, terminal = await read_effective_terminal(db, binding.user_id)
                                    await schedule_reports(db, binding, terminal.notifications, now)
                            await db.commit()
                        next_scan = elapsed + 10
                    await self.heartbeat()
                    if self.healthy:
                        for _ in range(3):
                            if not await self.dispatch_one():
                                break
                except TelegramError as exc:
                    self.healthy = False
                    self.details["error_code"] = exc.code
                    self.details["webhook_conflict"] = exc.code == "WEBHOOK_CONFLICT"
                    next_ready = elapsed_seconds() + 30
                    await self.heartbeat()
                except Exception as exc:
                    log.warning(
                        "notification_worker_iteration_failed", error_type=type(exc).__name__
                    )
                    self.details["error_code"] = "WORKER_DATABASE_OR_POLICY_ERROR"
                    with contextlib.suppress(Exception):
                        await self.heartbeat()
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=2)
                except TimeoutError:
                    pass
        finally:
            incoming.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await incoming
            self.healthy = False
            with contextlib.suppress(Exception):
                await self.heartbeat()


async def main():
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=2,
        max_overflow=1,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = TrustedClock()
    async with engine.connect() as lock:
        await lock.execution_options(isolation_level="AUTOCOMMIT")
        lock_pid = await lock.scalar(text("SELECT pg_backend_pid()"))
        if not await lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}):
            raise RuntimeError("Another notification worker holds the singleton lock")
        if (
            await lock.scalar(text("SELECT version_num FROM alembic_version"))
            != EXPECTED_SCHEMA_REVISION
        ):
            raise RuntimeError("Run migrations before notification worker")
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=4), trust_env=False
        ) as http:
            client = None
            initial_error = None
            try:
                if not settings.notification_worker_enabled:
                    raise TelegramError("WORKER_DISABLED")
                client = TelegramClient(http, read_bot_token(settings.telegram_token_file))
            except TelegramError as exc:
                initial_error = exc.code
            worker = NotificationWorker(
                settings, sessions, client, clock, singleton=(lock, lock_pid)
            )
            worker.details["error_code"] = initial_error
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, worker.stop.set)

            async def synchronize():
                while not worker.stop.is_set():
                    await clock.synchronize(http)
                    try:
                        await asyncio.wait_for(worker.stop.wait(), 30)
                    except TimeoutError:
                        pass

            clock_task = asyncio.create_task(synchronize())
            try:
                await worker.run()
            finally:
                clock_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await clock_task
        await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
