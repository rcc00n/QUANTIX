"""Bounded notification scanning; never mutates scanner evidence or financial state."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from arbiq.db.models import ArbitrageEvaluation, ArbitrageRoute
from arbiq.notifications.messages import alert_message
from arbiq.notifications.models import (
    AlertDecision,
    AlertLifecycle,
    NotificationOutbox,
    TelegramBinding,
)
from arbiq.notifications.service import audit, enqueue
from arbiq.settings.terminal import read_effective_terminal
from arbiq.signals.policy import EDMONTON, evaluate, timestamp


async def persistence(db, evaluation, policy, now, clock_trusted):
    """Count a continuous sequence of independent fresh rechecks, bounded to 64 rows."""
    rows = list(
        await db.scalars(
            select(ArbitrageEvaluation)
            .where(
                ArbitrageEvaluation.route_id == evaluation.route_id,
                ArbitrageEvaluation.evaluated_at <= evaluation.evaluated_at,
                ArbitrageEvaluation.evaluated_at >= now - timedelta(seconds=330),
            )
            .order_by(ArbitrageEvaluation.revision.desc())
            .limit(64)
        )
    )
    stripped = policy.model_copy(
        update={
            "minimum_persistence_seconds": 0,
            "quiet_hours_enabled": False,
            "emergency_alerts": False,
        }
    )
    latest = None
    previous = evaluation.evaluated_at
    earliest = previous
    for row in rows:
        if (previous - row.evaluated_at).total_seconds() > policy.maximum_quote_age_seconds:
            break
        if not all(
            row.evidence.get(k, {}).get("fresh_rest_recheck") is True
            for k in ("buy_confirmation", "sell_confirmation")
        ):
            break
        # Evaluate age at its original decision instant, with immutable evidence only.
        gate = evaluate(
            row.result, row.evidence, stripped, row.evaluated_at, clock_trusted=clock_trusted
        )
        if not gate.eligible or gate.classification != (
            "ACTIONABLE" if evaluation.result.get("actionable") is True else "RESEARCH_ONLY"
        ):
            break
        capture = (row.result.get("supporting_received_at"), row.result.get("supporting_source_at"))
        if capture != latest:
            earliest = row.evaluated_at
            latest = capture
        previous = row.evaluated_at
    return max(0, (evaluation.evaluated_at - earliest).total_seconds())


async def evaluate_source(db, source, policy, now, clock_trusted):
    duration = await persistence(db, source, policy, now, clock_trusted)
    clock = getattr(db, "info", {}).get("notification_clock")
    if clock is not None:
        now = clock.utcnow()
        clock_trusted = bool(clock_trusted and clock.snapshot().get("trusted"))
        db.info["last_alert_checked_at"] = now
    return evaluate(
        source.result,
        source.evidence,
        policy,
        now,
        clock_trusted=clock_trusted,
        persistence_seconds=duration,
    )


