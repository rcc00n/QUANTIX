"""Bounded retained valuation status for the authenticated owner's active portfolios."""

from collections import Counter

from sqlalchemy import select, true

from arbiq.db.models import PaperSession
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.models import PortfolioCheckpoint

VALUATION_STATES = ("COMPLETE_FRESH", "PARTIAL", "STALE", "UNKNOWN")
MAX_ACTIVE_PORTFOLIOS = 20


async def paper_valuation_health(db, owner_id):
    checkpoint = (
        select(
            PortfolioCheckpoint.as_of,
            PortfolioCheckpoint.payload["valuation_status"].as_string().label("valuation_status"),
        )
        .where(PortfolioCheckpoint.session_id == PaperSession.id)
        .order_by(PortfolioCheckpoint.as_of.desc(), PortfolioCheckpoint.id.desc())
        .limit(1)
        .lateral()
    )
    rows = (
        await db.execute(
            select(checkpoint.c.as_of, checkpoint.c.valuation_status)
            .select_from(PaperSession)
            .join(PaperControl, PaperSession.id == PaperControl.session_id)
            .outerjoin(checkpoint, true())
            .where(PaperSession.user_id == owner_id, PaperControl.status == "ACTIVE")
            .order_by(PaperSession.started_at.desc(), PaperSession.id.desc())
            .limit(MAX_ACTIVE_PORTFOLIOS + 1)
        )
    ).all()
    bounded = rows[:MAX_ACTIVE_PORTFOLIOS]
    counts = Counter(dict.fromkeys(VALUATION_STATES, 0))
    for _, state in bounded:
        counts[state if state in VALUATION_STATES else "UNKNOWN"] += 1
    return {
        "scope": "latest_retained_checkpoint_per_active_owner_portfolio",
        "portfolios_sampled": len(bounded),
        "truncated": len(rows) > MAX_ACTIVE_PORTFOLIOS,
        "valuation_counts": dict(counts),
        "latest_checkpoint_at": max((stamp for stamp, _ in bounded if stamp), default=None),
    }
