"""SYNTHETIC notification fixtures only. Never seed an application database or call Telegram."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arbiq.auth.security import hash_token
from arbiq.db.models import User
from arbiq.notifications.commands import handle_update
from arbiq.notifications.models import (
    NotificationEvent,
    NotificationOutbox,
    TelegramChallenge,
)
from arbiq.notifications.providers import (
    ProviderDisabled,
    push_provider,
    voice_provider,
)
from arbiq.notifications.reports import scheduled_period
from arbiq.notifications.service import (
    approve_pairing,
    create_challenge,
    enqueue,
    request_pairing,
    revoke_pairing,
)
from arbiq.notifications.telegram import TelegramClient, TelegramError, read_bot_token
from arbiq.notifications.worker import (
    NotificationWorker,
    failure_state,
    recover_sending,
    retry_delay,
)
from arbiq.settings.terminal import NotificationPreferences
from arbiq.signals.policy import evaluate, quiet_hours
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 22, 18, tzinfo=UTC)


def synthetic_result(**updates):
    result = {
        "synthetic": True,
        "actionable": False,
        "status": "RESEARCH_ONLY",
        "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
        "supporting_received_at": NOW.isoformat(),
        "supporting_source_at": NOW.isoformat(),
        "acquisition_cost_cad": "50",
        "selected_budget_cad": "10000",
        "net_profit_cad": "5",
        "net_return_pct": "10",
        "quantity": "0.5",
        "costs": {
            "sell_base_required": "0.5",
            "capital_requirements": {
                "synthetic-buy": {"CAD": "50"},
                "synthetic-sell": {"SYNTH": "0.5"},
            },
        },
        "actionability_confirmations": {
            key: True
            for key in (
                "eligibility",
                "funding",
                "fees",
                "fx",
                "market_limits",
                "canonical_identity",
            )
        },
    }
    return {**result, **updates}


def synthetic_evidence(**updates):
    return {
        "buy_quality": "HEALTHY",
        "sell_quality": "HEALTHY",
        "clock": {"trusted": True, "host_healthy": True, "uncertainty_seconds": "0.1"},
        **updates,
    }


def policy(**updates):
    return NotificationPreferences(
        telegram_enabled=True,
        research_enabled=True,
        quiet_hours_enabled=False,
        **updates,
    )


def gate(result=None, settings=None, **kwargs):
    return evaluate(
        result or synthetic_result(),
        synthetic_evidence(),
        settings or policy(),
        NOW,
        clock_trusted=True,
        **kwargs,
    )


def test_minimum_actual_buy_capital_is_independent_and_thresholds_are_all_required():
    assert gate().eligible
    for key, value, reason in (
        ("acquisition_cost_cad", "6.71", "BUY_CAPITAL_BELOW_MINIMUM"),
        ("acquisition_cost_cad", None, "BUY_CAPITAL_BELOW_MINIMUM"),
        ("net_profit_cad", "4.999999999999999999", "PROFIT_BELOW_MINIMUM"),
        ("net_return_pct", "0.499999999999999999", "RETURN_BELOW_MINIMUM"),
    ):
        check = gate(synthetic_result(**{key: value}))
        assert not check.eligible and reason in check.reasons
    assert gate(synthetic_result(acquisition_cost_cad="50.000000000000000001")).eligible
    assert (
        "BUY_CAPITAL_BELOW_MINIMUM"
        in gate(synthetic_result(acquisition_cost_cad=50.0)).reasons
    )


def test_research_is_disabled_by_default_and_never_emergency_even_when_opted_in():
    p = NotificationPreferences(telegram_enabled=True)
    assert "RESEARCH_NOTIFICATIONS_DISABLED" in gate(settings=p).reasons
    check = gate(settings=policy(emergency_alerts=True), persistence_seconds=100)
    assert (
        check.eligible
        and not check.emergency
        and check.classification == "RESEARCH_ONLY"
    )


def test_actionability_requires_every_explicit_confirmation():
    source = synthetic_result(actionable=True, status="VERIFIED_MARKET")
    assert gate(source).eligible
    missing = {**source, "actionability_confirmations": {}}
    assert not gate(missing).eligible
    assert all(
        any(key.upper() in reason for reason in gate(missing).reasons)
        for key in (
            "eligibility",
            "funding",
            "fees",
            "fx",
            "market_limits",
            "canonical_identity",
        )
    )


def test_freshness_expiry_and_clock_fail_closed():
    assert (
        "OPPORTUNITY_EXPIRED"
        in gate(synthetic_result(expires_at=NOW.isoformat())).reasons
    )
    assert (
        "QUOTE_STALE"
        in gate(
            synthetic_result(
                supporting_source_at=(NOW - timedelta(seconds=10)).isoformat()
            )
        ).reasons
    )
    assert (
        "QUOTE_TIME_UNAVAILABLE_OR_FUTURE"
        in gate(
            synthetic_result(
                supporting_source_at=(NOW + timedelta(milliseconds=1)).isoformat()
            )
        ).reasons
    )
    check = evaluate(
        synthetic_result(), synthetic_evidence(), policy(), NOW, clock_trusted=False
    )
    assert "UTC_CLOCK_UNTRUSTED" in check.reasons


def test_nighttime_requires_stricter_all_thresholds_and_persistence():
    now = NOW.replace(hour=7)  # Midnight Edmonton, November.
    p = NotificationPreferences(telegram_enabled=True, emergency_alerts=True)
    source = synthetic_result(
        actionable=True,
        status="VERIFIED_MARKET",
        acquisition_cost_cad="500",
        net_profit_cad="25",
        net_return_pct="5",
        expires_at=(now + timedelta(seconds=20)).isoformat(),
        supporting_received_at=now.isoformat(),
        supporting_source_at=now.isoformat(),
    )
    a = evaluate(
        source, synthetic_evidence(), p, now, clock_trusted=True, persistence_seconds=9
    )
    assert not a.eligible and not a.emergency and "EMERGENCY_PERSISTENCE" in a.reasons
    b = evaluate(
        source, synthetic_evidence(), p, now, clock_trusted=True, persistence_seconds=10
    )
    assert b.eligible and b.emergency and not b.silent
    for key, val in (("acquisition_cost_cad", "499.99"), ("net_profit_cad", "24.99")):
        assert not evaluate(
            {**source, key: val},
            synthetic_evidence(),
            p,
            now,
            clock_trusted=True,
            persistence_seconds=20,
        ).eligible


def test_edmonton_quiet_hours_dst_and_daily_weekly_idempotency_key():
    p = NotificationPreferences()
    for utc_hour in (7, 8):  # Both 01:30 during the fall DST fold.
        assert quiet_hours(datetime(2025, 11, 2, utc_hour, 30, tzinfo=UTC), p)
    assert scheduled_period(datetime(2025, 11, 3, 16, tzinfo=UTC), p)[2] == "2025-11-03"
    period = scheduled_period(datetime(2025, 11, 2, 16, tzinfo=UTC), p)
    assert (period[1] - period[0]).total_seconds() == 25 * 3600
    assert scheduled_period(datetime(2025, 11, 3, 15, 59, tzinfo=UTC), p) is None
    assert (
        scheduled_period(datetime(2025, 11, 2, 16, tzinfo=UTC), p, weekly=True) is None
    )


def test_backoff_rate_limits_and_delivery_ambiguity_are_explicit():
    assert retry_delay(1) == 2 and retry_delay(50) == 900
    assert retry_delay(2, 120) == 120
    assert failure_state(TelegramError("TELEGRAM_429", retry_after=3), 1) == "retry"
    assert failure_state(TelegramError("TELEGRAM_503"), 1) == "retry"
    assert failure_state(TelegramError("TELEGRAM_403"), 1) == "failed"
    assert failure_state(TelegramError("TELEGRAM_503"), 8) == "failed"
    assert (
        failure_state(TelegramError("TELEGRAM_NETWORK", ambiguous=True), 1)
        == "ambiguous"
    )


def test_token_permissions_validation_and_repr_are_redacted(tmp_path):
    path = tmp_path / "synthetic-token"
    token = "123456789:" + "SYNTHETIC_ONLY_" * 3
    path.write_text(token)
    path.chmod(0o644)
    with pytest.raises(TelegramError, match="TOKEN_FILE_PERMISSIONS"):
        read_bot_token(str(path))
    path.chmod(0o600)
    assert read_bot_token(str(path)) == token
    assert token not in repr(TelegramClient(None, token))
    assert token not in str(TelegramError("TELEGRAM_401"))


async def test_paid_providers_cannot_send():
    for provider in (push_provider, voice_provider):
        assert not provider.enabled
        with pytest.raises(ProviderDisabled):
            await provider.deliver(None)


async def synthetic_owner(db):
    user = User(
        username=f"synthetic-notifications-{uuid4().hex}",
        password_hash="SYNTHETIC_NO_AUTH",
    )
    db.add(user)
    await db.flush()
    return user


async def synthetic_pair(db, user, now=NOW):
    result = await create_challenge(db, user.id, now)
    # Telegram numeric IDs are synthetic and unique.
    identity = int(uuid4().hex[:12], 16)
    chat, sender = (
        {"type": "private", "id": identity},
        {"id": identity, "username": "synthetic_owner"},
    )
    assert await request_pairing(db, result["challenge"], chat, sender, now)
    return await approve_pairing(db, user.id, UUID(result["challenge_id"]), now)


from uuid import UUID


async def test_pairing_approval_requires_owner_private_chat_expiry_and_one_use(
    database_url,
):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            user, other = await synthetic_owner(db), await synthetic_owner(db)
            result = await create_challenge(db, user.id, NOW)
            challenge_id = UUID(result["challenge_id"])
            challenge = await db.get(TelegramChallenge, challenge_id)
            assert challenge.token_hash == hash_token(result["challenge"])
            assert result["challenge"] not in repr(challenge.__dict__)
            with pytest.raises(ValueError):
                await approve_pairing(db, user.id, challenge_id, NOW)
            assert not await request_pairing(
                db, result["challenge"], {"type": "group", "id": 12}, {"id": 12}, NOW
            )
            assert not await request_pairing(
                db, result["challenge"], {"type": "private", "id": 12}, {"id": 13}, NOW
            )
            identity = int(uuid4().hex[:12], 16)
            chat, sender = (
                {"type": "private", "id": identity},
                {"id": identity, "username": "synthetic"},
            )
            assert await request_pairing(db, result["challenge"], chat, sender, NOW)
            assert not await request_pairing(db, result["challenge"], chat, sender, NOW)
            with pytest.raises(ValueError):
                await approve_pairing(db, other.id, challenge_id, NOW)
            with pytest.raises(ValueError):
                await approve_pairing(
                    db, user.id, challenge_id, NOW + timedelta(minutes=11)
                )
            binding = await approve_pairing(db, user.id, challenge_id, NOW)
            assert binding.user_id == user.id
            with pytest.raises(ValueError):
                await approve_pairing(db, user.id, challenge_id, NOW)
            await db.rollback()
    finally:
        await engine.dispose()


class SyntheticClock:
    def utcnow(self):
        return NOW

    def snapshot(self):
        return {"trusted": True}


class SyntheticTelegram:
    def __init__(self, failure=None):
        self.failure, self.calls = failure, []

    async def send(self, chat_id, message, *, silent=False):
        self.calls.append((chat_id, message, silent))
        if self.failure:
            raise self.failure
        return {"message_id": 12345}


async def test_durable_outbox_retries_expiry_restart_dedup_and_revoke(database_url):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            user = await synthetic_owner(db)
            binding = await synthetic_pair(db, user)
            row = await enqueue(
                db,
                binding,
                key=f"synthetic:{uuid4()}",
                kind="test",
                message="SYNTHETIC delivery",
                now=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
            again = await enqueue(
                db,
                binding,
                key=row.deduplication_key,
                kind="test",
                message="SYNTHETIC ignored duplicate",
                now=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
            assert row.id == again.id
            await db.commit()
            row_id, user_id = row.id, user.id
        client = SyntheticTelegram(TelegramError("TELEGRAM_503"))
        worker = NotificationWorker(
            SimpleNamespace(), sessions, client, SyntheticClock()
        )
        # Tests run in an isolated database; select only this synthetic row in a savepoint-independent setup.
        assert await worker.dispatch_one()
        async with sessions() as db:
            row = await db.get(NotificationOutbox, row_id)
            assert row.status == "retry" and row.attempts == 1
            assert row.available_at > NOW
            row.available_at = NOW
            await db.commit()
        worker.last_send = float("-inf")
        client.failure = None
        assert await worker.dispatch_one()
        async with sessions() as db:
            row = await db.get(NotificationOutbox, row_id)
            assert row.status == "delivered" and row.provider_message_id == "12345"
            assert row.attempts == 2
            second = await enqueue(
                db,
                binding,
                key=f"synthetic:{uuid4()}",
                kind="test",
                message="SYNTHETIC interrupted",
                now=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
            second.status = "sending"
            third = await enqueue(
                db,
                binding,
                key=f"synthetic:{uuid4()}",
                kind="test",
                message="SYNTHETIC expired",
                now=NOW - timedelta(minutes=2),
                expires_at=NOW,
            )
            await db.commit()
            second_id, third_id = second.id, third.id
        async with sessions() as db:
            await recover_sending(db)
            await db.commit()
            assert (await db.get(NotificationOutbox, second_id)).status == "ambiguous"
        worker.last_send = float("-inf")
        assert await worker.dispatch_one()
        async with sessions() as db:
            assert (await db.get(NotificationOutbox, third_id)).status == "expired"
            pending = await enqueue(
                db,
                binding,
                key=f"synthetic:{uuid4()}",
                kind="test",
                message="SYNTHETIC revoked",
                now=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
            await revoke_pairing(db, user_id, NOW)
            await db.commit()
            assert pending.status == "cancelled"
        assert len(client.calls) == 2
    finally:
        await engine.dispose()


async def test_unauthorized_commands_never_queue_private_data_and_events_are_immutable(
    database_url,
):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            before = await db.scalar(
                select(func.count()).select_from(NotificationOutbox)
            )
            update = {
                "update_id": 42,
                "message": {
                    "text": "/portfolio",
                    "chat": {"id": -999, "type": "private"},
                    "from": {"id": -999},
                },
            }
            await handle_update(
                db, update, NOW, clock_trusted=True, app_url="http://synthetic.invalid"
            )
            assert (
                await db.scalar(select(func.count()).select_from(NotificationOutbox))
                == before
            )
            event = NotificationEvent(
                event_type="synthetic.test", details={"synthetic": True}
            )
            db.add(event)
            await db.commit()
            with pytest.raises(DBAPIError, match="append-only"):
                await db.execute(
                    text(
                        "UPDATE notification_events SET event_type='synthetic.invalid' WHERE id=:id"
                    ),
                    {"id": event.id},
                )
            await db.rollback()
    finally:
        await engine.dispose()


class SyntheticResponse:
    def __init__(self, body, status=200):
        import json

        self.raw, self.status, self.content = json.dumps(body).encode(), status, self

    async def read(self, size):
        return self.raw[:size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class SyntheticHttp:
    def __init__(self, responses):
        self.responses, self.methods = iter(responses), []

    def post(self, url, **kwargs):
        self.methods.append(url.rsplit("/", 1)[-1])
        assert kwargs["allow_redirects"] is False
        return next(self.responses)












