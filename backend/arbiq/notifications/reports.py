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


