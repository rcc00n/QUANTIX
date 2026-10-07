"""Private paper/portfolio APIs. No endpoint can place a real exchange order."""

from datetime import timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import func, or_, select

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import (
    Asset,
    Exchange,
    FeeVersion,
    FXQuote,
    Journal,
    LedgerEntry,
    PaperFill,
    PaperOrder,
    PaperSession,
    PaperTrade,
    SimulationEvent,
    StrategyVersion,
    SystemEvent,
    VirtualAccount,
)
from arbiq.finance.contracts import Contract, NonNegativeDecimal, PositiveDecimal
from arbiq.market_data.api import utc_reference
from arbiq.market_data.quality import safe_json
from arbiq.paper_trading.coordinator import trade_dto
from arbiq.paper_trading.lifecycle import lifecycle_rank
from arbiq.paper_trading.models import (
    ExecutionObservation,
    PaperControl,
    RebalanceRequest,
    StrategyHead,
)
from arbiq.paper_trading.rebalance_preview import RebalancePreview, estimate_transfer
from arbiq.paper_trading.service import Allocation, create_portfolio, session_dto
from arbiq.portfolio import accounting, analytics
from arbiq.portfolio.contracts import AccountingError, Reservation
from arbiq.portfolio.models import DailyAccountingSnapshot, PortfolioCheckpoint, PortfolioPosition
from arbiq.settings.strategy import PaperStrategy, presets, save_strategy

router = APIRouter(tags=["paper trading and portfolio"])


async def owned(db, user, session_id):
    row = (
        await db.execute(
            select(PaperSession, PaperControl)
            .join(PaperControl)
            .where(PaperSession.id == session_id, PaperSession.user_id == user.id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Paper portfolio not found")
    return row


async def now_checked(request):
    now, clock = await utc_reference(request)
    if not clock["trusted"]:
        raise HTTPException(503, "Trusted application UTC unavailable; paper writes paused")
    return now


def record(row):
    return safe_json({c.name: getattr(row, c.name) for c in row.__table__.columns})


class CreatePortfolio(Contract):
    name: str = Field(default="Paper portfolio", min_length=1, max_length=100)
    starting_capital_cad: PositiveDecimal = "10000"
    classification: Literal["VALIDATED", "RESEARCH"] | None = None
    settings: PaperStrategy | None = None
    allocations: list[Allocation] | None = None


@router.get("/paper/sessions")
async def sessions(db: DB, user: AuthenticatedUser):
    rows = (
        await db.execute(
            select(PaperSession, PaperControl)
            .join(PaperControl)
            .where(PaperSession.user_id == user.id)
            .order_by(PaperSession.started_at)
            .limit(100)
        )
    ).all()
    return {
        "items": [session_dto(*r) for r in rows],
        "total": len(rows),
        "scenarios_are_separate": True,
    }


@router.post("/paper/sessions", status_code=201)
async def create(body: CreatePortfolio, request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    settings = (
        body.settings
        or presets()["Aggressive Research" if body.classification == "RESEARCH" else "Balanced"]
    )
    settings = settings.model_copy(update={"starting_capital_cad": body.starting_capital_cad})
    if body.classification and (settings.mode == "RESEARCH_SHADOW") != (
        body.classification == "RESEARCH"
    ):
        raise HTTPException(422, "Research and validated capital require separate portfolio modes")
    try:
        result = await create_portfolio(
            db, user.id, body.name, settings, body.starting_capital_cad, body.allocations, now
        )
        await db.commit()
        return session_dto(*result)
    except (ValueError, AccountingError) as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from None


@router.get("/paper/sessions/{session_id}/portfolio")
async def portfolio(session_id: UUID, request: Request, db: DB, user: AuthenticatedUser):
    session, control = await owned(db, user, session_id)
    await db.commit()  # Ownership is read-only; release the pool slot before live IPC.
    client = request.app.state.collector_http
    if client:
        try:
            async with client.get(f"/paper/portfolios/{session_id}") as response:
                if response.status == 200:
                    value = await response.json()
                    value["storage_origin"] = "current_ledger_and_observed_marks"
                    return value
        except (aiohttp.ClientError, TimeoutError, ValueError):
            pass
    last = await db.scalar(
        select(PortfolioCheckpoint)
        .where(PortfolioCheckpoint.session_id == session_id)
        .order_by(PortfolioCheckpoint.as_of.desc())
        .limit(1)
    )
    if last:
        return {
            **last.payload,
            "storage_origin": "retained_snapshot",
            "stale": True,
            "valuation_status": "STALE",
            "valuation_status_at_checkpoint": last.payload.get("valuation_status"),
            "limitations": [*last.payload.get("limitations", []), "LIVE_VALUATION_UNAVAILABLE"],
        }
    return {
        "session_id": str(session.id),
        "classification": control.classification,
        "initial_equity_cad": str(session.initial_equity_cad),
        "equity_cad": None,
        "valuation_status": "UNKNOWN",
        "valued_subtotal_cad": None,
        "unpriced_balances": [],
        "net_simulated_pnl_cad": None,
        "realized_trading_pnl_cad": None,
        "realized_fx_pnl_cad": None,
        "unrealized_pnl_cad": None,
        "fees_cad": None,
        "expenses_cad": None,
        "no_trading_benchmark_cad": None,
        "drawdown_pct": None,
        "capital_utilization_pct": None,
        "portfolio_return_pct": None,
        "balances": [],
        "limitations": ["FIRST_VALUATION_PENDING"],
        "storage_origin": "unavailable",
    }


@router.get("/paper/sessions/{session_id}/metrics")
async def metrics(session_id: UUID, db: DB, user: AuthenticatedUser):
    _, control = await owned(db, user, session_id)
    value = await analytics.performance(
        db, session_id, research_only=control.classification == "RESEARCH"
    )
    value["counts"] = {
        **value.get("counts", {}),
        **{k.lower(): v for k, v in value.get("counts", {}).items()},
    }
    return value


@router.get("/paper/trades/{trade_id}")
async def detail(trade_id: UUID, db: DB, user: AuthenticatedUser):
    trade = await db.scalar(
        select(PaperTrade)
        .join(PaperSession)
        .where(PaperTrade.id == trade_id, PaperSession.user_id == user.id)
    )
    if not trade:
        raise HTTPException(404, "Paper trade not found")
    orders = list(await db.scalars(select(PaperOrder).where(PaperOrder.trade_id == trade_id)))
    fills = list(
        await db.scalars(
            select(PaperFill)
            .join(PaperOrder)
            .where(PaperOrder.trade_id == trade_id)
            .order_by(PaperFill.executed_at)
            .limit(100)
        )
    )
    events = list(
        await db.scalars(
            select(SimulationEvent)
            .where(SimulationEvent.trade_id == trade_id)
            .order_by(
                SimulationEvent.occurred_at,
                lifecycle_rank(SimulationEvent.event_type),
                SimulationEvent.id,
            )
            .limit(500)
        )
    )
    observations = list(
        await db.scalars(
            select(ExecutionObservation)
            .where(ExecutionObservation.trade_id == trade_id)
            .order_by(ExecutionObservation.received_at)
            .limit(200)
        )
    )
    journals = list(
        await db.scalars(
            select(Journal)
            .where(
                Journal.session_id == trade.session_id,
                or_(
                    Journal.source_id == trade.id,
                    Journal.idempotency_key.like("%:" + str(trade.id) + "%"),
                ),
            )
            .limit(200)
        )
    )
    entries = list(
        await db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.journal_id.in_([j.id for j in journals]))
            .limit(2000)
        )
    )
    accounts = list(
        await db.scalars(
            select(VirtualAccount).where(VirtualAccount.id.in_({e.account_id for e in entries}))
        )
    )
    fees = list(
        await db.scalars(
            select(FeeVersion).where(FeeVersion.id.in_({f.fee_version_id for f in fills}))
        )
    )
    rates = list(
        await db.scalars(
            select(FXQuote).where(FXQuote.id.in_({f.fx_quote_id for f in fills if f.fx_quote_id}))
        )
    )
    session = await db.get(PaperSession, trade.session_id)
    return {
        "schema_version": "paper-trade-evidence-v1",
        "trade": trade_dto(trade),
        "session_provenance": {
            "initial_equity_cad": str(session.initial_equity_cad),
            "benchmark": session.benchmark_snapshot,
        },
        "observations": [record(x) for x in observations],
        "journals": [record(x) for x in journals],
        "ledger_entries": [record(x) for x in entries],
        "accounts": [record(x) for x in accounts],
        "fee_versions": [record(x) for x in fees],
        "fx_quotes": [record(x) for x in rates],
        "record_limits": {"observations": 200, "journals": 200, "ledger_entries": 2000},
        "export_scope": "one simulation; full portfolio ZIP available in Analytics",
        "event_ordering": "UTC then lifecycle rank for exact ties; original timestamps unchanged",
        "orders": [record(x) for x in orders],
        "fills": [record(x) for x in fills],
        "events": [record(x) for x in events],
        "paper_only": True,
        "event_limit": 500,
    }


