"""Deterministic owner reports with independent session accounting and explicit unknowns."""

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select

from arbiq.core.clock import elapsed_seconds
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    PaperSession,
    SystemEvent,
)
from arbiq.notifications.messages import value
from arbiq.notifications.models import AlertDecision, NotificationOutbox
from arbiq.notifications.service import enqueue
from arbiq.portfolio.analytics import performance
from arbiq.portfolio.models import PortfolioCheckpoint
from arbiq.signals.policy import EDMONTON


def scheduled_period(now, policy, *, weekly=False):
    """Local schedule gives one report key through DST folds and missed worker ticks."""
    local = now.astimezone(EDMONTON)
    hour, minute = map(int, policy.daily_report_time.split(":"))
    due = datetime.combine(local.date(), time(hour, minute), EDMONTON)
    if local < due or (local - due).total_seconds() > 6 * 3600:
        return None
    if weekly and local.weekday() != policy.weekly_report_day:
        return None
    start = datetime.combine(
        local.date() - timedelta(days=7 if weekly else 1), time(hour, minute), EDMONTON
    )
    return start.astimezone(UTC), due.astimezone(UTC), local.date().isoformat()


async def report(db, user_id, start, end, *, weekly=False):
    detected = await db.scalar(
        select(func.count())
        .select_from(ArbitrageRoute)
        .where(ArbitrageRoute.detected_at >= start, ArbitrageRoute.detected_at < end)
    )
    # Count distinct routes, never inflate opportunities with evaluation revisions.
    actionable = await db.scalar(
        select(func.count(func.distinct(ArbitrageEvaluation.route_id))).where(
            ArbitrageEvaluation.evaluated_at >= start,
            ArbitrageEvaluation.evaluated_at < end,
            ArbitrageEvaluation.result["actionable"].as_boolean().is_(True),
        )
    )
    research = await db.scalar(
        select(func.count(func.distinct(ArbitrageEvaluation.route_id))).where(
            ArbitrageEvaluation.evaluated_at >= start,
            ArbitrageEvaluation.evaluated_at < end,
            ArbitrageEvaluation.result["actionable"].as_boolean().is_(False),
        )
    )
    sent = await db.scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(
            NotificationOutbox.user_id == user_id,
            NotificationOutbox.status == "delivered",
            NotificationOutbox.delivered_at >= start,
            NotificationOutbox.delivered_at < end,
            NotificationOutbox.kind.in_(("alert", "research", "emergency")),
        )
    )
    expired = await db.scalar(
        select(func.count())
        .select_from(AlertDecision)
        .where(
            AlertDecision.user_id == user_id,
            AlertDecision.eligible.is_(True),
            AlertDecision.expires_at >= start,
            AlertDecision.expires_at < end,
        )
    )
    expired_opportunities = await db.scalar(
        select(func.count(func.distinct(ArbitrageLifecycleEvent.route_id))).where(
            ArbitrageLifecycleEvent.status == "EXPIRED",
            ArbitrageLifecycleEvent.event_at >= start,
            ArbitrageLifecycleEvent.event_at < end,
        )
    )
    expired_before_delivery = await db.scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(
            NotificationOutbox.user_id == user_id,
            NotificationOutbox.status == "expired",
            NotificationOutbox.expires_at >= start,
            NotificationOutbox.expires_at < end,
        )
    )
    problems = list(
        await db.scalars(
            select(SystemEvent.event_type)
            .where(
                SystemEvent.severity.in_(("warning", "error")),
                SystemEvent.created_at >= start,
                SystemEvent.created_at < end,
            )
            .order_by(SystemEvent.created_at.desc())
            .limit(5)
        )
    )
    sessions = list(
        await db.scalars(
            select(PaperSession)
            .where(PaperSession.user_id == user_id)
            .order_by(PaperSession.started_at)
            .limit(21)
        )
    )
    if len(sessions) > 20:
        raise ValueError("REPORT_SESSION_LIMIT_EXCEEDED")
    lines = [
        f"QUANTIX — {'WEEKLY' if weekly else 'DAILY'} PAPER REPORT",
        f"{start.isoformat()} → {end.isoformat()}",
        f"Detected: {detected}; actionable: {actionable}; research-only: {research}",
        f"Alerts delivered: {sent}; eligible alerts expired: {expired}",
        f"Expired opportunities: {expired_opportunities}",
        f"Expired before delivery: {expired_before_delivery}",
    ]
    for session in sessions:
        metrics = await performance(db, session.id, period_start=start, period_end=end)
        trading = metrics
        checkpoint = await db.scalar(
            select(PortfolioCheckpoint)
            .where(PortfolioCheckpoint.session_id == session.id, PortfolioCheckpoint.as_of < end)
            .order_by(PortfolioCheckpoint.as_of.desc())
            .limit(1)
        )
        p = checkpoint.payload if checkpoint else {}
        completed = trading.get("completed_in_period", trading.get("completed_simulations", 0))
        lines.extend(
            [
                f"\n{session.name[:80]} (independent simulated capital)",
                f"Completed simulations: {completed}; "
                f"missed: {trading.get('missed_simulations', 0)}; partial: "
                f"{trading.get('partially_completed', 0)}",
                f"Completed matched-leg net P&L: "
                f"C${value(trading.get('net_completed_trade_pnl_cad'))}",
                f"Retained equity: C${value(p.get('equity_cad'))}; unrealized: "
                f"C${value(p.get('unrealized_pnl_cad'))}",
                f"Retained simulated net P&L: C${value(p.get('net_simulated_pnl_cad'))}; "
                f"realized disposal/FX P&L: C${value(p.get('realized_simulated_pnl_cad'))}",
                f"Original-holdings benchmark: C${value(p.get('no_trading_benchmark_cad'))}; "
                f"strategy excess: C${value(p.get('strategy_excess_cad'))}",
                f"Valuation as of: {checkpoint.as_of.isoformat() if checkpoint else 'unavailable'}",
            ]
        )
    if not sessions:
        lines.append("Completed simulations: 0. No funded simulated portfolios.")
    lines.append(
        "Major system problems: "
        + (", ".join(dict.fromkeys(problems)) or "none recorded in period")
    )
    lines.append(
        "PAPER ONLY. Simulated results are not real-world realized profit. Valuations are "
        "retained evidence."
    )
    text = "\n".join(lines)
    if len(text) > 3900:
        raise ValueError("REPORT_MESSAGE_LIMIT_EXCEEDED")
    return text


