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


