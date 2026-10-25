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


async def cooldown_reason(db, user_id, source, policy, now, emergency):
    seconds = policy.escalation_cooldown_seconds if emergency else policy.cooldown_seconds
    prior = await db.scalar(
        select(AlertDecision.id)
        .join(ArbitrageEvaluation, AlertDecision.evaluation_id == ArbitrageEvaluation.id)
        .where(
            AlertDecision.user_id == user_id,
            AlertDecision.eligible.is_(True),
            ArbitrageEvaluation.route_id == source.route_id,
            AlertDecision.evaluated_at >= now - timedelta(seconds=seconds),
        )
        .limit(1)
    )
    if prior:
        return "ROUTE_COOLDOWN"
    if emergency:
        local = now.astimezone(EDMONTON)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        count = await db.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(
                NotificationOutbox.user_id == user_id,
                NotificationOutbox.kind == "emergency",
                NotificationOutbox.created_at >= start,
                NotificationOutbox.status.not_in(("cancelled", "expired", "failed")),
            )
        )
        if count >= policy.daily_emergency_cap:
            return "DAILY_EMERGENCY_CAP"
    return None


async def scan(db, now, *, clock_trusted, app_url):
    bindings = list(
        await db.scalars(
            select(TelegramBinding)
            .where(TelegramBinding.revoked_at.is_(None), TelegramBinding.paused.is_(False))
            .limit(10)
        )
    )
    if not bindings:
        return
    # One current immutable source per route, with explicit work bounds.
    recent = (
        select(ArbitrageEvaluation)
        .distinct(ArbitrageEvaluation.route_id)
        .where(ArbitrageEvaluation.evaluated_at >= now - timedelta(seconds=30))
    )
    recent = recent.order_by(
        ArbitrageEvaluation.route_id, ArbitrageEvaluation.revision.desc()
    ).subquery()
    sources = list(
        await db.scalars(
            select(ArbitrageEvaluation)
            .join(recent, recent.c.id == ArbitrageEvaluation.id)
            .order_by(ArbitrageEvaluation.evaluated_at.desc())
            .limit(32)
        )
    )
    for binding in bindings:
        settings_row, terminal = await read_effective_terminal(db, binding.user_id)
        policy = terminal.notifications
        if not policy.telegram_enabled:
            continue
        for source in sources:
            version = settings_row.version if settings_row else 0
            key = f"evaluation:{binding.user_id}:{source.id}:{version}"
            if await db.scalar(
                select(AlertDecision.id).where(AlertDecision.deduplication_key == key)
            ):
                continue
            gate = await evaluate_source(db, source, policy, now, clock_trusted)
            decision_at = db.info.get("last_alert_checked_at", now)
            reasons = list(gate.reasons)
            if gate.eligible:
                cooldown = await cooldown_reason(
                    db, binding.user_id, source, policy, now, gate.emergency
                )
                if cooldown:
                    reasons.append(cooldown)
            expiry = timestamp(source.result.get("expires_at")) or now
            decision = AlertDecision(
                user_id=binding.user_id,
                evaluation_id=source.id,
                settings_version_id=settings_row.id if settings_row else None,
                deduplication_key=key,
                evaluated_at=decision_at,
                expires_at=expiry,
                classification=gate.classification,
                eligible=not reasons,
                emergency=gate.emergency,
                reasons=reasons,
                rules=policy.model_dump(mode="json"),
                evidence={
                    "alert_eligibility_decided_at": decision_at.isoformat(),
                    "batch_reference_at": now.isoformat(),
                    "source_evaluated_at": source.evaluated_at.isoformat(),
                    "calculation_version": source.calculation_version,
                    "route_id": str(source.route_id),
                    "revision": source.revision,
                    "buy_snapshot_id": str(source.buy_snapshot_id),
                    "sell_snapshot_id": str(source.sell_snapshot_id),
                    "buy_fee_version_id": str(source.buy_fee_version_id),
                    "sell_fee_version_id": str(source.sell_fee_version_id),
                },
            )
            db.add(decision)
            await db.flush()
            if reasons:
                continue
            db.add(AlertLifecycle(decision_id=decision.id, repeat_count=0))
            await enqueue(
                db,
                binding,
                key=f"alert:{decision.id}:0",
                kind="emergency"
                if gate.emergency
                else "research"
                if gate.classification == "RESEARCH_ONLY"
                else "alert",
                message=alert_message(source.result, now, app_url, emergency=gate.emergency),
                now=now,
                expires_at=expiry,
                alert_id=decision.id,
                silent=gate.silent,
            )


