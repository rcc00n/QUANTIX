"""Bounded historical analytics; no price interpolation and no future marks."""

from datetime import UTC, timedelta

from fastapi import HTTPException
from sqlalchemy import Numeric, cast, func, or_, select

from arbiq.analytics.contracts import ReportPeriod
from arbiq.db.models import (
    ArbitrageRoute,
    Journal,
    LedgerEntry,
    PaperSession,
    PaperTrade,
    StrategyVersion,
    VirtualAccount,
)
from arbiq.market_data.quality import safe_json
from arbiq.notifications.models import AlertDecision, NotificationEvent, NotificationOutbox
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.analytics import performance
from arbiq.portfolio.contracts import stored
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint

MAX_REPORT_DAYS = 366
SERIES_KEYS = (
    "equity_cad",
    "no_trading_benchmark_cad",
    "net_simulated_pnl_cad",
    "realized_simulated_pnl_cad",
    "unrealized_pnl_cad",
    "drawdown_pct",
    "capital_utilization_pct",
    "fees_cad",
    "expenses_cad",
    "strategy_excess_cad",
)


def period_for(session, start, end, now):
    end = end or now
    start = start or max(session.started_at, end - timedelta(days=7))
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "Reporting timestamps require explicit UTC offsets")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start or end - start > timedelta(days=MAX_REPORT_DAYS):
        raise HTTPException(422, "Reporting period must be positive and at most366days")
    if end > now + timedelta(seconds=1):
        raise HTTPException(422, "Reporting cutoff cannot be in the future")
    return ReportPeriod(start=start, end=end)


async def owned_session(db, user_id, session_id, strategy_version_id=None):
    row = (
        await db.execute(
            select(PaperSession, PaperControl)
            .join(PaperControl)
            .where(PaperSession.id == session_id, PaperSession.user_id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Paper portfolio not found")
    if strategy_version_id is not None:
        version = await db.scalar(
            select(StrategyVersion).where(
                StrategyVersion.id == strategy_version_id, StrategyVersion.user_id == user_id
            )
        )
        if version is None:
            raise HTTPException(404, "Strategy version not found")
    return row


