"""Autonomous PAPER ONLY simulation task under the collector singleton lease.

First valid post-delay observations are pinned on arrival. Each leg commits its own
journal; delayed transfers cannot sell until settlement and a new observation.
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import func, select

from arbiq.core.clock import elapsed_seconds
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageRoute,
    Asset,
    FeeVersion,
    FXQuote,
    Market,
    PaperFill,
    PaperOrder,
    PaperSession,
    PaperTrade,
    SimulationEvent,
    User,
)
from arbiq.market_data.quality import CapturedBook, safe_json
from arbiq.opportunities.analysis import dump_input, load_input
from arbiq.opportunities.contracts import Costs, Fee
from arbiq.opportunities.math import convert, optimize_route
from arbiq.opportunities.storage import OpportunityStore
from arbiq.paper_trading.execution import (
    VERSION,
    evidence_eligibility,
    floor_derived,
    planned_reserves,
    recheck_projection,
    simulate_leg,
)
from arbiq.paper_trading.market import PaperMarkets
from arbiq.paper_trading.models import ExecutionObservation, PaperControl, RebalanceRequest
from arbiq.paper_trading.service import Allocation, create_portfolio
from arbiq.portfolio import accounting, analytics, valuation
from arbiq.portfolio.contracts import (
    AccountingError,
    InsufficientBalance,
    Reservation,
    decimal_context,
    stored,
)
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint, PortfolioPosition
from arbiq.settings.strategy import minimum_buy_satisfied, presets, read_pinned_strategy

log = structlog.get_logger()
ACTIVE = {"SCHEDULED", "RECHECKING", "EXECUTING"}
TERMINAL = {"COMPLETED", "PARTIAL", "MISSED", "FAILED", "EXPIRED"}


def allowed_paper_asset(allowed, ticker, canonical):
    """Keep legacy ticker filters and match canonical plan IDs against pinned evidence."""
    return not allowed or any((canonical if ":" in asset else ticker) == asset for asset in allowed)


def event(db, trade, status, now, reason="", details=None):
    trade.status = status
    trade.reason = reason or None
    if status in TERMINAL:
        trade.completed_at = now
    db.add(
        SimulationEvent(
            trade_id=trade.id,
            order_id=None,
            event_type=status,
            reason=reason or status,
            occurred_at=now,
            provenance=safe_json(details or {}),
        )
    )


def trade_dto(t):
    return {
        "id": str(t.id),
        "session_id": str(t.session_id),
        "route_id": str(t.route_id) if t.route_id else None,
        "evaluation_id": str(t.evaluation_id) if t.evaluation_id else None,
        "status": t.status,
        "classification": t.classification,
        "mode": t.mode,
        "requested_at": t.requested_at,
        "eligible_at": t.eligible_at,
        "completed_at": t.completed_at,
        "reason": t.reason,
        "requested_quantity": str(t.requested_quantity)
        if t.requested_quantity is not None
        else None,
        "reaction_delay_ms": t.reaction_delay_ms,
        "state": t.state,
        "assumptions": t.assumptions,
        "strategy_version_id": str(t.strategy_version_id) if t.strategy_version_id else None,
    }


class PaperCoordinator:
    def __init__(self, collector):
        self.collector = collector
        self.config = collector.settings.paper
        self.sessions = collector.store.sessions
        self.market = PaperMarkets(collector)
        self.targets = {}
        self.recent = defaultdict(lambda: deque(maxlen=16))
        self.store = OpportunityStore(self.sessions, collector.store.run_id)
        self.last_tick = 0.0
        self.last_valuation = 0.0
        self.metrics = defaultdict(int)
        self.daily_hours = {}

    def clock(self):
        return (
            self.collector.utc_clock.snapshot()
            if self.collector.utc_clock
            else {"trusted": False, "host_healthy": False}
        )

    async def observe(self, book):
        if book.market_id not in self.targets:
            return
        self.recent[book.market_id].append(book)
        clock = self.clock()
        for target in list(self.targets.get(book.market_id, ())):
            if book.received_at < target["eligible_at"]:
                continue
            if (
                target.get("raw_due")
                and target["run_id"] == str(self.collector.store.run_id)
                and elapsed_seconds() < target["raw_due"]
            ):
                continue
            strategy = read_pinned_strategy(target["settings"])
            allowed, reason = evidence_eligibility(
                book,
                now=self.collector.utcnow(),
                eligible_at=target["eligible_at"],
                maximum_age=strategy.maximum_book_age_seconds,
                research=target["research"],
                allow_missing=strategy.allow_missing_source_timestamp,
                minimum_confidence=strategy.minimum_confidence,
                clock_trusted=clock["trusted"],
            )
            if not allowed:
                self.metrics["rejected_observations"] += 1
                continue
            references = [
                b
                for b in self.recent.get(target.get("opposite_id"), ())
                if b.received_at <= book.received_at and b.ingestion_index <= book.ingestion_index
            ]
            opposite = references[-1].model_dump(mode="json") if references else None
            fx = self.collector.scanner.fx_routes(book.received_at)
            fx = {
                k: dump_input(v)
                for k, v in fx.items()
                if v.available_at is None or v.available_at <= book.received_at
            }
            async with self.sessions() as db:
                trade = await db.get(PaperTrade, target["trade_id"])
                order = await db.scalar(
                    select(PaperOrder).where(PaperOrder.id == target["order_id"]).with_for_update()
                )
                if (
                    not trade
                    or trade.status not in ACTIVE
                    or not order
                    or order.status != "pending"
                ):
                    continue
                if await db.scalar(
                    select(ExecutionObservation.id)
                    .where(ExecutionObservation.order_id == order.id)
                    .limit(1)
                ):
                    continue
                sid = await self.store.snapshot(db, book)
                # Captured here before awaiting future observations. No later FX
                # update is used to price an earlier execution.
                db.add(
                    ExecutionObservation(
                        trade_id=trade.id,
                        order_id=order.id,
                        snapshot_id=sid,
                        received_at=book.received_at,
                        source_at=book.exchange_timestamp,
                        payload={
                            "book": book.model_dump(mode="json"),
                            "clock": clock,
                            "fx": fx,
                            "first_valid_after_delay": True,
                            "opposite_book_asof": opposite,
                            "eligibility": reason,
                        },
                    )
                )
                await db.commit()
                self.metrics["causal_observations"] += 1

