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












