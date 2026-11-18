"""Archive a settled simulation while retaining its financial evidence."""

from sqlalchemy import func, select

from arbiq.db.models import PaperSession, PaperTrade, SystemEvent
from arbiq.paper_trading.models import PaperControl, RebalanceRequest
from arbiq.portfolio import accounting
from arbiq.portfolio.models import PortfolioPosition

UNSETTLED = ("pending", "CANDIDATE", "ELIGIBILITY_CHECK", "SCHEDULED", "RECHECKING", "EXECUTING")
ARCHIVE_REBALANCE_LIMIT = 20


class PortfolioLifecycleError(ValueError):
    """The requested transition is incompatible with the current portfolio state."""


class PortfolioNotFound(PortfolioLifecycleError):
    """This session does not belong to the authenticated owner."""


async def lock_portfolio(db, user_id, session_id):
    """Serialize lifecycle and reserve mutations on the same session as accounting."""
    row = (
        await db.execute(
            select(PaperSession, PaperControl)
            .join(PaperControl)
            .where(PaperSession.id == session_id, PaperSession.user_id == user_id)
            .with_for_update(of=(PaperSession, PaperControl))
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise PortfolioNotFound("Paper portfolio not found")
    return row


async def cancel_pending_rebalances(db, session_id, now):
    """Cancel unperformed inventory transfers through the accounting journal."""
    rows = await db.scalars(
        select(RebalanceRequest)
        .where(RebalanceRequest.session_id == session_id, RebalanceRequest.status == "PENDING")
        .order_by(RebalanceRequest.requested_at, RebalanceRequest.id)
        .limit(ARCHIVE_REBALANCE_LIMIT)
        .execution_options(populate_existing=True)
    )
    for row in rows:
        reason = {"reason": "PORTFOLIO_ARCHIVE", "request_id": str(row.id)}
        release = await accounting.release_remaining(
            db,
            session_id,
            f"rebalance:{row.id}:reserve",
            now,
            f"rebalance:{row.id}:release",
            reason,
        )
        row.status = "CANCELLED"
        row.result = {
            **reason,
            "cancelled_at": now.isoformat(),
            "release_journal_id": str(release) if release is not None else None,
        }
    await db.flush()


async def archive_portfolio(db, user_id, session_id, now, *, require_paused=False):
    """Pause admission and archive after reserved trade legs settle normally.

    The caller owns the commit. Cohort publication may require an already paused
    session so a failed replacement cannot suspend currently active portfolios.
    """
    session, control = await lock_portfolio(db, user_id, session_id)
    if session.ended_at is not None:
        return session, control, {}
    if control.status != "PAUSED":
        if require_paused:
            raise PortfolioLifecycleError("Pause and settle the replaced paper portfolio first")
        control.status = "PAUSED"
        control.updated_at = now
        db.add(
            SystemEvent(
                actor_user_id=user_id,
                event_type="paper.archive_requested",
                severity="info",
                message="Paper admission paused for archival",
                details={"session_id": str(session_id)},
            )
        )
    await cancel_pending_rebalances(db, session_id, now)
    pending = {}
    for key, model, conditions in (
        ("attempts", PaperTrade, (PaperTrade.status.in_(UNSETTLED),)),
        ("rebalances", RebalanceRequest, (RebalanceRequest.status == "PENDING",)),
        ("reserved_positions", PortfolioPosition, (PortfolioPosition.reserved_quantity != 0,)),
    ):
        count = await db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.session_id == session_id, *conditions)
        )
        if count:
            pending[key] = count
    if pending:
        return session, control, pending
    if not await accounting.reconcile(db, session_id):
        raise PortfolioLifecycleError("Portfolio ledger must reconcile before archival")
    session.ended_at = now
    control.updated_at = now
    db.add(
        SystemEvent(
            actor_user_id=user_id,
            event_type="paper.archived",
            severity="info",
            message="Settled paper portfolio archived",
            details={"session_id": str(session_id)},
        )
    )
    return session, control, {}
