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


async def reconciliation(db, session_id, end):
    entries = (
        select(
            LedgerEntry.journal_id,
            VirtualAccount.asset_id,
            func.sum(LedgerEntry.quantity).label("quantity"),
        )
        .join(VirtualAccount, VirtualAccount.id == LedgerEntry.account_id)
        .join(Journal, Journal.id == LedgerEntry.journal_id)
        .where(Journal.session_id == session_id, Journal.effective_at < end)
        .group_by(LedgerEntry.journal_id, VirtualAccount.asset_id)
        .subquery()
    )
    unbalanced = await db.scalar(
        select(func.count()).select_from(entries).where(entries.c.quantity != 0)
    )
    accounts = (
        select(VirtualAccount.id, func.sum(LedgerEntry.quantity).label("quantity"))
        .join(LedgerEntry, LedgerEntry.account_id == VirtualAccount.id)
        .join(Journal, Journal.id == LedgerEntry.journal_id)
        .where(
            Journal.session_id == session_id,
            Journal.effective_at < end,
            VirtualAccount.role.in_(("available", "reserved")),
        )
        .group_by(VirtualAccount.id)
        .subquery()
    )
    negative = await db.scalar(
        select(func.count()).select_from(accounts).where(accounts.c.quantity < 0)
    )
    return {
        "scope": "all session journals effective before cutoff, including opening history",
        "balanced": unbalanced == 0 and negative == 0,
        "unbalanced_journal_assets": unbalanced,
        "negative_inventory_accounts": negative,
    }


async def analytics(db, session, control, period, strategy_version_id=None):
    metrics = await performance(
        db,
        session.id,
        research_only=control.classification == "RESEARCH",
        period_start=period.start,
        period_end=period.end,
        strategy_version_id=strategy_version_id,
    )
    opening = await db.scalar(
        select(PortfolioCheckpoint)
        .where(
            PortfolioCheckpoint.session_id == session.id,
            PortfolioCheckpoint.as_of <= period.start,
        )
        .order_by(PortfolioCheckpoint.as_of.desc(), PortfolioCheckpoint.id.desc())
        .limit(1)
    )
    portfolio = metrics.get("portfolio")
    # Durable checkpoints are historical evidence, not a claim of current live prices.
    limitations = list(portfolio.get("limitations", [])) if portfolio else ["NO_RETAINED_VALUATION"]
    if portfolio:
        limitations.append("PORTFOLIO_IS_RETAINED_CHECKPOINT_AT_OR_BEFORE_REPORT_CUTOFF")
    if strategy_version_id:
        limitations.append(
            "STRATEGY_FILTER_SCOPES_TRADE_RESULTS_ONLY_PORTFOLIO_AND_LEDGER_ARE_WHOLE_SESSION"
        )
    detections = await db.scalar(
        select(func.count())
        .select_from(ArbitrageRoute)
        .where(
            ArbitrageRoute.detected_at >= period.start,
            ArbitrageRoute.detected_at < period.end,
        )
    )
    efficiency = metrics.get("pnl_per_mean_reserved_capital_pct")
    versions = list(
        await db.scalars(
            select(StrategyVersion)
            .where(
                StrategyVersion.user_id == session.user_id,
                or_(
                    StrategyVersion.id.in_(
                        [session.strategy_version_id, control.strategy_version_id]
                    ),
                    StrategyVersion.id.in_(
                        select(PaperTrade.strategy_version_id).where(
                            PaperTrade.session_id == session.id,
                            PaperTrade.requested_at < period.end,
                        )
                    ),
                ),
            )
            .order_by(StrategyVersion.version.desc())
            .limit(1001)
        )
    )
    reconciled = await reconciliation(db, session.id, period.end)
    reconciled["portfolio_delta_cad"] = (
        portfolio.get("reconciliation_delta_cad") if portfolio else None
    )
    reconciled["portfolio_checkpoint_as_of"] = portfolio.get("as_of") if portfolio else None
    reconciled["portfolio_equity_reconciled"] = (
        portfolio.get("reconciliation_delta_cad") in ("0", "0E-18", "0.000000000000000000")
        if portfolio and portfolio.get("reconciliation_delta_cad") is not None
        else None
    )
    expense_groups = {}
    for event, amount, known, count in (
        await db.execute(
            select(
                AccountingMovement.event_type,
                func.sum(AccountingMovement.expenses_cad),
                func.count(AccountingMovement.expenses_cad),
                func.count(),
            )
            .where(
                AccountingMovement.session_id == session.id,
                AccountingMovement.effective_at >= period.start,
                AccountingMovement.effective_at < period.end,
            )
            .group_by(AccountingMovement.event_type)
        )
    ).all():
        expense_groups[event] = stored(amount) if known == count else None
    expense_breakdown = {
        "scope": "Whole-session ledger accounting effective in selected period",
        "trading_fees_cad": metrics["accounting"]["fees_cad"],
        "transfer_rebalancing_expenses_cad": expense_groups.get("INVENTORY_TRANSFER", "0"),
        "fx_conversion_costs_cad": None,
        "fx_costs_reason": "No separate FX-cost ledger category; spread remains in rate evidence",
        "realized_fx_pnl_cad": metrics["accounting"]["realized_fx_pnl_cad"],
        "completed_trade_modeled_slippage_cad": metrics["completed_trade_slippage_cad"],
        "warning": "Disclosures already affect cashflows/basis; do not deduct them twice",
    }
    notification_counts = {
        "scope": "Owner network notifications in period; not attributed to portfolio or strategy",
        "decisions": await db.scalar(select(func.count()).select_from(AlertDecision).where(
            AlertDecision.user_id == session.user_id,
            AlertDecision.evaluated_at >= period.start, AlertDecision.evaluated_at < period.end,
        )),
        "eligible": await db.scalar(select(func.count()).select_from(AlertDecision).where(
            AlertDecision.user_id == session.user_id, AlertDecision.eligible.is_(True),
            AlertDecision.evaluated_at >= period.start, AlertDecision.evaluated_at < period.end,
        )),
        "research_decisions": await db.scalar(select(func.count()).select_from(AlertDecision).where(
            AlertDecision.user_id == session.user_id,
            AlertDecision.classification == "RESEARCH_ONLY",
            AlertDecision.evaluated_at >= period.start, AlertDecision.evaluated_at < period.end,
        )),
        "telegram_alerts_delivered": await db.scalar(
            select(func.count()).select_from(NotificationOutbox).where(
                NotificationOutbox.user_id == session.user_id,
                NotificationOutbox.kind.in_(("alert", "research", "emergency")),
                NotificationOutbox.delivered_at >= period.start,
                NotificationOutbox.delivered_at < period.end,
            )
        ),
        "expired_before_delivery": await db.scalar(
            select(func.count(func.distinct(NotificationEvent.outbox_id))).where(
                NotificationEvent.user_id == session.user_id,
                NotificationEvent.event_type == "delivery.expired",
                NotificationEvent.created_at >= period.start,
                NotificationEvent.created_at < period.end,
            )
        ),
    }
    return safe_json(
        {
            "schema_version": "analytics-v1",
            "paper_only": True,
            "session_id": str(session.id),
            "name": session.name,
            "classification": control.classification,
            "period": period.model_dump(mode="json"),
            "strategy_version_id": str(strategy_version_id) if strategy_version_id else None,
            "metrics": metrics,
            "portfolio": portfolio,
            "opening_portfolio": opening.payload if opening else None,
            "initial_equity_cad": session.initial_equity_cad,
            "no_trading_initial_conditions": session.benchmark_snapshot,
            "opportunity_detection_count": detections,
            "opportunity_count_scope": "Network public detections; no strategy P&L attribution",
            "notification_counts": notification_counts,
            "return_on_mean_committed_capital_pct": efficiency,
            "by_trade_size": metrics.get("by_trade_size", []),
            "expense_breakdown": expense_breakdown,
            "strategy_versions": [
                {
                    "id": str(v.id),
                    "version": v.version,
                    "created_at": v.created_at,
                    "preset": v.settings.get("preset", "Custom"),
                }
                for v in versions[:1000]
            ],
            "strategy_version_window_limited": len(versions) > 1000,
            "reconciliation": reconciled,
            "limitations": list(dict.fromkeys(limitations)),
            "accounting_convention": "Moving CAD basis; fees in cashflows, never subtracted twice",
            "reporting_timezone": "America/Edmonton",
            "currency": "CAD",
            "statistical_warning": (
                "Completed known-CAD simulations only; small samples are not forecasts."
            ),
        }
    )


