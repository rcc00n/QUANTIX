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


