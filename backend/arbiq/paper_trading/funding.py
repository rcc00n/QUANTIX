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


@router.get("/coverage")
async def coverage(
    session_id: UUID,
    request: Request,
    db: DB,
    user: AuthenticatedUser,
    limit: int = Query(20, ge=1, le=50),
):
    now = await now_checked(request)
    session, control = await owned(db, user, session_id)
    positions = list(
        await db.scalars(
            select(PortfolioPosition).where(PortfolioPosition.session_id == session_id)
        )
    )
    available = {
        (str(x.exchange_id), str(x.asset_id)): x.quantity - x.reserved_quantity for x in positions
    }
    latest = (
        select(ArbitrageEvaluation.id)
        .where(
            ArbitrageEvaluation.route_id == ArbitrageRoute.id,
            ArbitrageEvaluation.evaluated_at <= now,
        )
        .order_by(ArbitrageEvaluation.evaluated_at.desc(), ArbitrageEvaluation.id)
        .limit(1)
        .correlate(ArbitrageRoute)
        .scalar_subquery()
    )
    pairs = list(
        (
            await db.execute(
                select(ArbitrageRoute, ArbitrageEvaluation)
                .join(ArbitrageEvaluation, ArbitrageEvaluation.id == latest)
                .where(
                    ArbitrageRoute.status.in_(
                        ["DETECTED", "RECHECKING", "RESEARCH_ONLY", "VERIFIED_MARKET"]
                    ),
                    ArbitrageRoute.expires_at > now,
                    ArbitrageRoute.updated_at <= now,
                )
                .order_by(ArbitrageRoute.updated_at.desc())
                .limit(limit + 1)
            )
        ).all()
    )
    mids = {mid for r, _ in pairs for mid in (r.buy_market_id, r.sell_market_id)}
    markets = {x.id: x for x in await db.scalars(select(Market).where(Market.id.in_(mids)))}
    exchanges, assets = await metadata(
        db,
        {a for m in markets.values() for a in (m.base_asset_id, m.quote_asset_id)}
        | {p.asset_id for p in positions},
    )
    strategy = read_pinned_strategy(control.settings)
    output = []
    for route, evaluation in pairs[:limit]:
        buy, sell = markets[route.buy_market_id], markets[route.sell_market_id]
        reasons = []
        requirements = []
        market_net = None
        funded = None
        reallocatable = None
        limits = []
        try:
            best, cash, base = await asyncio.to_thread(required_quantities, evaluation, strategy)
            for m, asset_id, qty in (
                (buy, buy.quote_asset_id, cash),
                (sell, sell.base_asset_id, base),
            ):
                if m is sell and (
                    strategy.mode == "TRANSFER"
                    or (
                        strategy.mode == "RESEARCH_SHADOW"
                        and strategy.research_execution_model == "TRANSFER"
                    )
                ):
                    continue
                requirements.append(
                    {
                        "exchange_id": str(m.exchange_id),
                        "exchange": exchanges[m.exchange_id].slug,
                        "asset_id": str(asset_id),
                        "asset": assets[asset_id].symbol,
                        "canonical_key": assets[asset_id].canonical_key,
                        "required_quantity": str(qty),
                    }
                )
            required = missing_inventory(requirements, available)
            sufficient = all(Decimal(x["missing_quantity"]) == 0 for x in required)
            if not sufficient:
                reasons.append("INSUFFICIENT_PRE_FUNDED_INVENTORY")
            quantity = str(best.buy_quantity)
            market_net = best.net_profit_cad
        except (ValueError, KeyError, TypeError, ArithmeticError):
            required, sufficient, quantity = [], False, None
            reasons.append("CALCULATION_EVIDENCE_INCOMPLETE")
        if strategy.mode == "PRE_FUNDED" or (
            strategy.mode == "RESEARCH_SHADOW" and strategy.research_execution_model == "PRE_FUNDED"
        ):
            try:
                pair = read_pair(evaluation, now, strategy)
                buy_free = available.get(
                    (str(buy.exchange_id), str(buy.quote_asset_id)), Decimal(0)
                )
                sell_free = available.get(
                    (str(sell.exchange_id), str(sell.base_asset_id)), Decimal(0)
                )
                funded = size_at_budget(
                    pair,
                    strategy.trade_amount_cad,
                    buy_available=buy_free,
                    sell_available=sell_free,
                    contribution_marks=False,
                )
                if funded is None or funded.acquisition_cad < strategy.min_buy_capital_cad:
                    funded = None
                    limits.append("NO_USEFUL_FUNDED_SIZE")
                limits.extend(
                    "BUY_QUOTE_BALANCE" if index == 0 else "SELL_BASE_BALANCE"
                    for index, item in enumerate(required)
                    if Decimal(item["missing_quantity"]) > 0
                )
                target = size_at_budget(pair, strategy.trade_amount_cad)
                if target is not None:
                    paired_value = stored(target.buy_quote * target.buy_mark_rate) + stored(
                        target.sell_base * target.sell_mark_rate
                    )
                    reallocatable = (
                        not sufficient
                        and paired_value <= session.initial_equity_cad
                        and target.acquisition_cad >= strategy.min_buy_capital_cad
                    )
                if not limits:
                    limits.append("DEPTH_FX_PRECISION_OR_TRADE_CAP")
            except (AttributeError, ValueError, KeyError, TypeError, ArithmeticError):
                limits.append("EXACT_FUNDED_SIZE_EVIDENCE_UNAVAILABLE")
        output.append(
            {
                "route_id": str(route.id),
                "evaluation_id": str(evaluation.id),
                "asset": assets[buy.base_asset_id].symbol,
                "buy_exchange": exchanges[buy.exchange_id].slug,
                "sell_exchange": exchanges[sell.exchange_id].slug,
                "evaluated_at": evaluation.evaluated_at.isoformat(),
                "quantity": quantity,
                "required": required,
                "inventory_sufficient": sufficient,
                "projected_net_profit_cad": str(market_net) if market_net is not None else None,
                "market_profitability": "UNKNOWN"
                if market_net is None
                else ("PROJECTED_POSITIVE" if market_net > 0 else "NON_POSITIVE"),
                "maximum_funded_acquisition_cad": str(funded.acquisition_cad) if funded else None,
                "funded_size_net_profit_cad": str(funded.evaluation.net_profit_cad)
                if funded
                else None,
                "projected_costs": projected_costs(funded.evaluation) if funded else None,
                "limiting_constraints": limits,
                "fundable_with_reallocation": reallocatable,
                "reasons": reasons,
                "actionable": False,
                "classification": "RESEARCH",
            }
        )
    balances = [
        {
            "exchange": exchanges[p.exchange_id].slug,
            "asset": assets[p.asset_id].symbol,
            "canonical_key": assets[p.asset_id].canonical_key,
            "quantity": str(p.quantity),
            "available_quantity": str(p.quantity - p.reserved_quantity),
            "reserved_quantity": str(p.reserved_quantity),
        }
        for p in positions
    ]
    return {
        "session_id": str(session.id),
        "classification": control.classification,
        "balances": balances,
        "routes": output,
        "window": {"limit": limit, "limited": len(pairs) > limit},
        "limitations": [
            "Pinned projected quantities are not executable quotes",
            "Funding sufficiency does not verify fees, eligibility or clock",
        ],
    }


class ProposalRequest(Contract):
    capital_cad: PositiveDecimal = Field(default="10000", le=10000000)
    trade_amount_cad: PositiveDecimal = Field(default="250", le=1000000)
    exchange_slugs: tuple[str, ...] = ()
    asset_keys: tuple[str, ...] = ()
    policy: Literal[
        "COVERAGE", "EQUAL_VENUES", "LIQUIDITY_WEIGHTED", "ROUTE_OPTIMIZED", "DIVERSIFIED_HYBRID"
    ] = "COVERAGE"
    lookback_days: int = Field(default=7, ge=1, le=30)
    reserve_cash_pct: NonNegativeDecimal = Field(default="10", le=100)
    min_buy_capital_cad: NonNegativeDecimal = Field(default="50", le=1000000)
    min_cell_cad: NonNegativeDecimal = Field(default="50", le=1000000)
    max_venue_pct: PositiveDecimal = Field(default="60", le=100)
    max_asset_pct: PositiveDecimal = Field(default="70", le=100)
    max_reserve_pct: NonNegativeDecimal = Field(default="50", le=100)
    max_stablecoin_pct: NonNegativeDecimal = Field(default="50", le=100)
    min_venues: int = Field(default=2, ge=1, le=30)
    eligibility: Literal["RESEARCH", "VALIDATED"] = "RESEARCH"
    planning_cutoff: datetime | None = None
    inventory_asset_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounded(self):
        if (
            len(self.exchange_slugs) > 30
            or max(len(self.asset_keys), len(self.inventory_asset_keys)) > 100
        ):
            raise ValueError("Bounded funding filters required")
        if self.planning_cutoff and self.planning_cutoff.utcoffset() != timedelta(0):
            raise ValueError("Planning cutoff must be timezone-aware UTC")
        return self


async def quote_marks(request, quantities):
    client = request.app.state.collector_http
    if client is None or not quantities:
        return {}
    try:
        async with client.post("/paper/funding/marks", json={"items": quantities}) as response:
            if response.status != 200:
                return {}
            data = await response.json()
            if not data.get("clock", {}).get("trusted"):
                return {}
            return {(x["exchange_id"], x["asset_id"]): x for x in data.get("items", [])}
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
        return {}


@decimal_context
async def allocate_weighted(capital, weights, rates, full_marks, exchanges, assets, cash_exchange):
    """Allocate at sourced marks and keep every rounding/depth shortfall as CAD cash."""
    total_weight = sum(weights.values(), Decimal(0))
    items, used = [], Decimal(0)
    for key, weight in sorted(weights.items()):
        budget = floor_value(capital * weight / total_weight) if total_weight else Decimal(0)
        ex, asset = key
        rate = rates.get(key)
        if not rate or budget <= 0:
            continue
        quantity = quantity_for_budget(budget, rate)
        if quantity is None:
            continue
        mark = full_marks.get(key, {})
        if assets[UUID(asset)].canonical_key != "fiat:CAD":
            try:
                current = floor_value(Decimal(mark["cad_per_unit"]))
                if (
                    not mark.get("received_at")
                    or current <= 0
                    or Decimal(mark.get("quantity", "0")) != quantity
                ):
                    continue
                rate = current
            except (ValueError, KeyError, TypeError, ArithmeticError):
                continue
        # Match the established NUMERIC contribution convention used by
        # create_portfolio/fund. The budget guard still prevents rounding up
        # beyond allocated capital; the remaining exact units stay CAD cash.
        value = stored(quantity * rate)
        if quantity <= 0 or value <= 0 or value > budget:
            continue
        used += value
        items.append(
            {
                "exchange": exchanges[UUID(ex)].slug,
                "asset": assets[UUID(asset)].canonical_key,
                "quantity": str(quantity),
                "cad_rate": str(rate),
                "cad_value": str(value),
                "evidence": {
                    "source": "OBSERVED_RESEARCH_INITIALIZATION",
                    "mark": mark,
                    "hypothetical_initial_inventory": True,
                },
            }
        )
    remainder = capital - used
    if remainder > 0:
        items.append(
            {
                "exchange": cash_exchange.slug,
                "asset": "fiat:CAD",
                "quantity": str(remainder),
                "cad_rate": "1",
                "cad_value": str(remainder),
                "evidence": {"source": "CAD_IDENTITY", "cash_remainder": True},
            }
        )
    return items


