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


@decimal_context
def missing_inventory(requirements, available):
    """Exact unit deficits, never silently converted through stablecoin parity."""
    result = []
    for requirement in requirements:
        key = requirement["exchange_id"], requirement["asset_id"]
        needed = Decimal(requirement["required_quantity"])
        held = available.get(key, Decimal(0))
        result.append(
            {
                **requirement,
                "available_quantity": str(held),
                "missing_quantity": str(max(Decimal(0), needed - held)),
            }
        )
    return result


@decimal_context
def required_quantities(evaluation, strategy):
    inputs = evaluation.evidence.get("calculation_inputs", {})
    buy, sell = load_input(inputs["buy"]), load_input(inputs["sell"])
    buy = replace(
        buy, fee=Fee(max(buy.fee.rate, strategy.taker_fee_rate), strategy.buy_fee_currency, False)
    )
    sell = replace(
        sell,
        fee=Fee(max(sell.fee.rate, strategy.taker_fee_rate), strategy.sell_fee_currency, False),
    )
    optimized = optimize_route(
        buy,
        sell,
        budget_cad=floor_value(strategy.trade_amount_cad / (1 + strategy.slippage_bps / 10000)),
        now=evaluation.evaluated_at,
        costs=Costs(slippage_bps=strategy.slippage_bps, rebalancing_bps=strategy.rebalance_bps),
        budgets=(),
        max_search_evaluations=128,
    )
    best = optimized.best
    if best is None:
        raise ValueError("NO_FEASIBLE_PINNED_SIZE")
    quote, base = planned_reserves(best, buy.fee, sell.fee, strategy.slippage_bps)
    if quote <= 0 or base <= 0:
        raise ValueError("NO_STORABLE_PINNED_SIZE")
    return best, quote, base


async def metadata(db, asset_ids=()):
    exchanges = {x.id: x for x in await db.scalars(select(Exchange))}
    assets = {
        x.id: x
        for x in await db.scalars(
            select(Asset).where(Asset.id.in_(asset_ids) | (Asset.canonical_key == "fiat:CAD"))
        )
    }
    return exchanges, assets


@router.get("/catalog")
async def catalog(db: DB, user: AuthenticatedUser):
    exchanges = await db.scalars(
        select(Exchange).where(Exchange.enabled.is_(True)).order_by(Exchange.name).limit(30)
    )
    # Canonical monitored assets only; avoid sending all21k discovered listings.
    ids = (
        select(Market.base_asset_id)
        .join(LatestMarketBook)
        .union(select(Market.quote_asset_id).join(LatestMarketBook))
    )
    assets = await db.scalars(
        select(Asset).where(Asset.id.in_(ids)).order_by(Asset.symbol).limit(300)
    )
    return {
        "exchanges": [{"id": str(x.id), "slug": x.slug, "name": x.name} for x in exchanges],
        "assets": [
            {"id": str(x.id), "canonical_key": x.canonical_key, "symbol": x.symbol, "kind": x.kind}
            for x in assets
        ],
        "limits": {
            "maximum_allocations": 100,
            "maximum_active_portfolios": 20,
            "asset_window": 300,
        },
        "classification": "RESEARCH",
    }


