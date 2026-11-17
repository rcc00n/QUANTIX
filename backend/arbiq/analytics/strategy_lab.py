"""Read-only comparison of independent forward paper portfolios."""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import Numeric, case, cast, func, select

from arbiq.analytics.contracts import ReportPeriod
from arbiq.analytics.service import reconciliation
from arbiq.db.models import PaperSession, PaperTrade, SimulationEvent, StrategyVersion
from arbiq.finance.contracts import Contract, UTCTimestamp
from arbiq.market_data.quality import safe_json
from arbiq.paper_trading.lifecycle import LIFECYCLE_RANK, lifecycle_rank
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.contracts import stored
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint

CAPITALS = frozenset({Decimal(10000), Decimal(50000), Decimal(100000)})
MAX_WINDOW = timedelta(days=31)
MAX_BOUNDARY_AGE = timedelta(minutes=10)
RECONCILIATION_TOLERANCE = Decimal("0.000000000001")
INITIAL_FUNDING = "INITIAL_FUNDING"
ZERO = Decimal(0)


class StrategyLabRequest(Contract):
    session_ids: tuple[UUID, ...] = Field(min_length=2, max_length=4)
    start: UTCTimestamp
    end: UTCTimestamp

    @model_validator(mode="after")
    def comparable(self):
        if len(set(self.session_ids)) != len(self.session_ids):
            raise ValueError("Select distinct independent portfolios")
        if not timedelta(0) < self.end - self.start <= MAX_WINDOW:
            raise ValueError("Select a positive UTC interval of at most 31 days")
        return self


async def owned_cohort(db, owner_id, body: StrategyLabRequest):
    rows = (
        await db.execute(
            select(PaperSession, PaperControl, StrategyVersion)
            .join(PaperControl, PaperControl.session_id == PaperSession.id)
            .join(StrategyVersion, StrategyVersion.id == PaperSession.strategy_version_id)
            .where(
                PaperSession.id.in_(body.session_ids),
                PaperSession.user_id == owner_id,
                StrategyVersion.user_id == owner_id,
            )
        )
    ).all()
    if len(rows) != len(body.session_ids):
        raise HTTPException(404, "A selected paper portfolio was not found")
    capitals = {session.initial_equity_cad for session, _, _ in rows}
    if len(capitals) != 1 or not capitals <= CAPITALS:
        raise HTTPException(
            422, "Select independent portfolios with equal CAD 10,000, 50,000, or 100,000"
        )
    for session, control, version in rows:
        if control.classification != "RESEARCH":
            raise HTTPException(422, "Strategy Lab compares independent research portfolios")
        if session.strategy_version_id != control.strategy_version_id:
            raise HTTPException(422, "Select portfolios with an unchanged pinned strategy version")
        if max(session.started_at, session.created_at, version.created_at) > body.start or (
            session.ended_at is not None and session.ended_at < body.end
        ):
            raise HTTPException(422, "The entire UTC window must follow each portfolio's creation")
        if not session.benchmark_snapshot.get("holdings"):
            raise HTTPException(
                422, "Original benchmark holdings are unavailable for a selected portfolio"
            )
    funding = (
        await db.execute(
            select(
                AccountingMovement.session_id,
                func.sum(AccountingMovement.external_flow_cad),
                func.count(AccountingMovement.external_flow_cad),
                func.count(),
            )
            .where(
                AccountingMovement.session_id.in_(body.session_ids),
                AccountingMovement.event_type == INITIAL_FUNDING,
                AccountingMovement.effective_at <= body.start,
                AccountingMovement.created_at <= body.start,
            )
            .group_by(AccountingMovement.session_id)
        )
    ).all()
    capital = next(iter(capitals))
    contribution = {
        session_id: amount for session_id, amount, known, count in funding if known == count
    }
    if any(contribution.get(session_id) != capital for session_id in body.session_ids):
        raise HTTPException(
            422, "Exact initial contributions must be recorded by the shared opening cutoff"
        )
    by_id = {session.id: (session, version) for session, _, version in rows}
    return capital, [by_id[session_id] for session_id in body.session_ids]


async def trade_counts(db, session, period):
    recent = (
        select(SimulationEvent.event_type)
        .where(
            SimulationEvent.trade_id == PaperTrade.id,
            SimulationEvent.occurred_at < period.end,
            SimulationEvent.created_at < period.end,
            SimulationEvent.event_type.in_(tuple(LIFECYCLE_RANK)),
        )
        .order_by(
            SimulationEvent.occurred_at.desc(),
            lifecycle_rank(SimulationEvent.event_type).desc(),
            SimulationEvent.created_at.desc(),
            SimulationEvent.id.desc(),
        )
        .limit(1)
        .correlate(PaperTrade)
        .scalar_subquery()
    )
    status = func.coalesce(recent, "OUTCOME_EVIDENCE_UNAVAILABLE")
    rows = (
        await db.execute(
            select(
                status,
                func.count(),
                func.count().filter(
                    PaperTrade.strategy_version_id.is_distinct_from(session.strategy_version_id)
                ),
            )
            .where(
                PaperTrade.session_id == session.id,
                PaperTrade.classification == "RESEARCH",
                PaperTrade.requested_at >= period.start,
                PaperTrade.requested_at < period.end,
                PaperTrade.created_at < period.end,
            )
            .group_by(status)
        )
    ).all()
    if any(wrong_version for _, _, wrong_version in rows):
        raise HTTPException(422, "A selected portfolio has mixed strategy versions in this window")
    return {status: count for status, count, _ in rows}


async def accounting(db, session_id, period):
    movement = AccountingMovement
    realized = (
        movement.realized_trading_cad,
        movement.realized_fx_cad,
        movement.fees_cad,
        movement.expenses_cad,
        movement.external_flow_cad,
    )
    columns = [func.count()]
    for value in realized:
        columns.extend((func.sum(value), func.count(value)))
    columns.append(
        func.count().filter(
            (movement.external_flow_cad != ZERO) | movement.external_flow_cad.is_(None)
        )
    )
    # Funding exactly at the opening is admitted only when known by the
    # opening cutoff. Additional contributions are never performance.
    rows = (
        await db.execute(
            select(*columns).where(
                movement.session_id == session_id,
                movement.effective_at >= period.start,
                movement.effective_at < period.end,
                movement.created_at < period.end,
                ~(
                    (movement.event_type == INITIAL_FUNDING)
                    & (movement.effective_at == period.start)
                    & (movement.created_at <= period.start)
                ),
            )
        )
    ).one()
    values = [stored(rows[i] or ZERO) if rows[i + 1] == rows[0] else None for i in range(1, 11, 2)]
    result = dict(
        zip(
            (
                "realized_trading_pnl_cad",
                "realized_fx_pnl_cad",
                "fees_cad",
                "expenses_cad",
                "external_flow_cad",
            ),
            values,
            strict=True,
        )
    )
    result["unexpected_capital_flow_count"] = rows[-1]
    return result


async def boundary(db, session_id, cutoff, *, opening):
    checkpoint = PortfolioCheckpoint
    at = (checkpoint.as_of <= cutoff) if opening else (checkpoint.as_of < cutoff)
    recorded = (checkpoint.created_at <= cutoff) if opening else (checkpoint.created_at < cutoff)
    return await db.scalar(
        select(checkpoint)
        .where(checkpoint.session_id == session_id, at, recorded)
        .order_by(checkpoint.as_of.desc(), checkpoint.created_at.desc(), checkpoint.id.desc())
        .limit(1)
    )


def decimal_mark(payload, field) -> Decimal | None:
    value = payload.get(field)
    if not isinstance(value, (str, Decimal)):
        return None
    try:
        number = Decimal(value)
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def boundary_summary(checkpoint, cutoff):
    if checkpoint is None:
        return {
            "as_of": None,
            "recorded_at": None,
            "equity_cad": None,
            "benchmark_cad": None,
            "valuation_status": "UNKNOWN",
            "equity_reconciled": None,
        }
    row = checkpoint.payload
    age = cutoff - checkpoint.as_of
    equity = decimal_mark(row, "equity_cad")
    benchmark = decimal_mark(row, "no_trading_benchmark_cad")
    status = row.get("valuation_status") or ("COMPLETE_FRESH" if equity is not None else "UNKNOWN")
    delta = decimal_mark(row, "reconciliation_delta_cad")
    reconciled = abs(delta) <= RECONCILIATION_TOLERANCE if delta is not None else None
    if age > MAX_BOUNDARY_AGE:
        status = "STALE"
        equity = benchmark = None
    elif reconciled is False or status in {"PARTIAL", "STALE", "UNKNOWN"}:
        equity = None
        if reconciled is False:
            status = "PARTIAL"
    elif status == "COMPLETE_FRESH" and (equity is None or benchmark is None):
        status = "PARTIAL"
    return {
        "as_of": checkpoint.as_of,
        "recorded_at": checkpoint.created_at,
        "equity_cad": equity,
        "benchmark_cad": benchmark,
        "valuation_status": status,
        "equity_reconciled": reconciled,
    }


async def sample_metrics(db, session_id, period, opening_equity):
    checkpoint = PortfolioCheckpoint
    payload = checkpoint.payload
    latest = (
        select(
            checkpoint.as_of,
            case(
                (
                    payload["valuation_status"].as_string().in_(("PARTIAL", "STALE", "UNKNOWN")),
                    None,
                ),
                else_=cast(payload["equity_cad"].as_string(), Numeric(38, 18)),
            ).label("equity"),
            cast(payload["capital_utilization_pct"].as_string(), Numeric(38, 18)).label(
                "utilization"
            ),
        )
        .where(
            checkpoint.session_id == session_id,
            checkpoint.as_of > period.start,
            checkpoint.as_of < period.end,
            checkpoint.created_at < period.end,
        )
        .distinct(checkpoint.as_of)
        .order_by(checkpoint.as_of, checkpoint.created_at.desc(), checkpoint.id.desc())
        .subquery()
    )
    ordered = select(
        latest.c.equity,
        latest.c.utilization,
        func.greatest(
            opening_equity or ZERO,
            func.max(latest.c.equity).over(order_by=latest.c.as_of, rows=(None, 0)),
        ).label("peak"),
    ).subquery()
    count, valued, drawdown, utilization, utilized = (
        await db.execute(
            select(
                func.count(),
                func.count(ordered.c.equity),
                func.max(
                    (ordered.c.peak - ordered.c.equity) * 100 / func.nullif(ordered.c.peak, 0)
                ),
                func.avg(ordered.c.utilization),
                func.count(ordered.c.utilization),
            )
        )
    ).one()
    return {
        "retained_checkpoint_count": count,
        "complete_checkpoint_count": valued,
        "drawdown_pct": stored(drawdown)
        if opening_equity is not None and count and valued == count
        else None,
        "capital_utilization_pct": stored(utilization)
        if count and utilized == count and valued == count
        else None,
    }


def window_returns(opening, closing, *, ledger_reconciled, external_flow):
    values = {
        k: None
        for k in (
            "portfolio_return_pct",
            "benchmark_return_pct",
            "excess_return_pct",
            "strategy_excess_cad",
        )
    }
    if not ledger_reconciled or external_flow != ZERO:
        return values
    first, last = opening["equity_cad"], closing["equity_cad"]
    baseline, later = opening["benchmark_cad"], closing["benchmark_cad"]
    with localcontext() as ctx:
        ctx.prec = 78
        if first is not None and first > 0 and last is not None:
            values["portfolio_return_pct"] = stored((last - first) / first * 100)
        if baseline is not None and baseline > 0 and later is not None:
            values["benchmark_return_pct"] = stored((later - baseline) / baseline * 100)
        if (
            values["portfolio_return_pct"] is not None
            and values["benchmark_return_pct"] is not None
        ):
            values["excess_return_pct"] = stored(
                (last - first) / first * 100 - (later - baseline) / baseline * 100
            )
            values["strategy_excess_cad"] = stored((last - first) - (later - baseline))
    return values


async def strategy_cohort(db, owner_id: UUID, body: StrategyLabRequest, now: datetime):
    if body.end > now:
        raise HTTPException(422, "The UTC cutoff cannot be in the future")
    capital, sessions = await owned_cohort(db, owner_id, body)
    period = ReportPeriod(start=body.start, end=body.end)
    output = []
    for session, version in sessions:
        counts = await trade_counts(db, session, period)
        money = await accounting(db, session.id, period)
        opening_checkpoint = await boundary(db, session.id, period.start, opening=True)
        closing_checkpoint = await boundary(db, session.id, period.end, opening=False)
        opening = boundary_summary(opening_checkpoint, period.start)
        closing = boundary_summary(closing_checkpoint, period.end)
        proof = await reconciliation(db, session.id, period.end, causal=True)
        samples = await sample_metrics(db, session.id, period, opening["equity_cad"])
        limitations = []
        if counts.get("OUTCOME_EVIDENCE_UNAVAILABLE"):
            limitations.append(
                "Some requested paper events lack a causal retained lifecycle record"
            )
        if (
            opening["valuation_status"] != "COMPLETE_FRESH"
            or closing["valuation_status"] != "COMPLETE_FRESH"
        ):
            limitations.append("One or both boundary valuations are partial, stale or unavailable")
        reconciled = proof["balanced"]
        if not reconciled:
            limitations.append("Ledger reconciliation failed at cutoff")
        flow_free = money["unexpected_capital_flow_count"] == 0
        if not flow_free:
            limitations.append(
                "An additional or unresolved capital flow prevents comparable period returns"
            )
        returns = window_returns(
            opening,
            closing,
            ledger_reconciled=reconciled,
            external_flow=ZERO if flow_free else None,
        )
        closing_payload = closing_checkpoint.payload if closing_checkpoint else {}
        latest_valued = reconciled and closing["equity_cad"] is not None
        if not reconciled or not flow_free or closing["equity_cad"] is None:
            samples["drawdown_pct"] = None
            samples["capital_utilization_pct"] = None
        output.append(
            {
                "session_id": str(session.id),
                "name": session.name,
                "profile": version.settings.get("strategy_lab_profile") or "BASELINE",
                "strategy_version_id": str(version.id),
                "strategy_version": version.version,
                "started_at": session.started_at,
                "initial_capital_cad": capital,
                "opening": opening,
                "closing": closing,
                "valuation_status": closing["valuation_status"],
                "completed_simulations": counts.get("COMPLETED", 0),
                "partially_completed": counts.get("PARTIAL", 0),
                "failed_simulations": counts.get("FAILED", 0),
                "missed_simulations": counts.get("MISSED", 0),
                "expired_simulations": counts.get("EXPIRED", 0),
                "paper_activity": counts,
                **{key: amount if reconciled else None for key, amount in money.items()},
                "unrealized_pnl_cad": decimal_mark(closing_payload, "unrealized_pnl_cad")
                if latest_valued
                else None,
                "inventory_exposure_cad": decimal_mark(closing_payload, "inventory_exposure_cad")
                if latest_valued
                else None,
                "largest_exchange_share_pct": decimal_mark(
                    closing_payload, "largest_exchange_share_pct"
                )
                if latest_valued
                else None,
                **returns,
                **samples,
                "ledger_reconciled": reconciled,
                "limitations": limitations,
            }
        )
    return safe_json(
        {
            "schema_version": "quantix-strategy-lab-v1",
            "kind": "FORWARD_PAPER_OBSERVED",
            "paper_only": True,
            "period": period.model_dump(mode="json"),
            "initial_capital_cad": capital,
            "rows": output,
            "limitations": [
                "Independent portfolios are compared at equal initial contributions. "
                "Each keeps its own holdings benchmark.",
                "Paper activity uses trades requested in the shared UTC interval. "
                "Accounting uses effective movements recorded by cutoff.",
                "Returns use retained opening and closing marks within ten minutes of each cutoff. "
                "Drawdown uses retained samples only.",
                "Forward paper outcomes stay separate from counterfactual historical replay.",
            ],
        }
    )
