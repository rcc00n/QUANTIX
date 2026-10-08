"""Capital-constrained hypothetical funding. No deposits or real orders are submitted."""

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Literal
from uuid import UUID

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import aliased, defer

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageRoute,
    Asset,
    Exchange,
    LatestMarketBook,
    Market,
    OrderBookSnapshot,
    User,
)
from arbiq.finance.contracts import Contract, NonNegativeDecimal, PositiveDecimal
from arbiq.opportunities.analysis import load_input
from arbiq.opportunities.contracts import Costs, Fee
from arbiq.opportunities.coverage import venue_group
from arbiq.opportunities.math import optimize_route
from arbiq.paper_trading import allocation
from arbiq.paper_trading.api import now_checked, owned
from arbiq.paper_trading.execution import planned_reserves
from arbiq.paper_trading.funding_plans import HistoricalRoute, funding_demands
from arbiq.paper_trading.funding_sizing import (
    projected_costs,
    read_pair,
    size_at_budget,
    time_value,
)
from arbiq.paper_trading.service import Allocation, create_portfolio, session_dto
from arbiq.portfolio.contracts import AccountingError, decimal_context, stored
from arbiq.portfolio.models import PortfolioPosition
from arbiq.settings.models import FundingExperiment, FundingProposal
from arbiq.settings.strategy import PaperStrategy, presets, read_pinned_strategy

router = APIRouter(prefix="/paper/funding", tags=["virtual funding planner"])
STEP = Decimal("1e-18")
PLANNING_LIMIT = asyncio.Lock()
OPTIMIZED_POLICIES = ("LIQUIDITY_WEIGHTED", "ROUTE_OPTIMIZED", "DIVERSIFIED_HYBRID")


def floor_value(value):
    with localcontext() as ctx:
        ctx.prec = 78
        return stored(value.quantize(STEP, rounding=ROUND_DOWN))


@decimal_context
def quantity_for_budget(budget, rate):
    try:
        if budget <= 0 or rate <= 0:
            return None
        value = floor_value(budget / rate)
        return value if value > 0 else None
    except (ValueError, ArithmeticError):
        return None


