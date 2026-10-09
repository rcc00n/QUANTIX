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


@decimal_context
async def allocate_complete_routes(
    capital, requirements, mark_source, exchanges, assets, cash_exchange
):
    """Greedily admit both inventory legs, never one funded half of a route.

    Shared cells use their largest alternative-route requirement. This is single
    route coverage, not a promise that all routes can reserve funds concurrently.
    """
    if capital <= 0:
        return []
    selected, marks = {}, {}
    for route in requirements[:30]:
        if not route.get("required"):
            continue
        candidate = dict(selected)
        for item in route["required"]:
            key = item["exchange_id"], item["asset_id"]
            candidate[key] = max(candidate.get(key, Decimal(0)), Decimal(item["required_quantity"]))
        if len(candidate) > 40:
            route["reasons"].append("ALLOCATION_CELL_LIMIT")
            continue
        changes = {
            key: quantity
            for key, quantity in candidate.items()
            if key not in selected or selected[key] != quantity
        }
        needed = [
            {"exchange_id": ex, "asset_id": asset, "quantity": str(quantity)}
            for (ex, asset), quantity in changes.items()
            if assets[UUID(asset)].canonical_key != "fiat:CAD"
        ]
        fresh = await mark_source(needed) if needed else {}
        trial_marks = dict(marks)
        possible = True
        for (ex, asset), quantity in changes.items():
            key = ex, asset
            if assets[UUID(asset)].canonical_key == "fiat:CAD":
                trial_marks[key] = {
                    "cad_per_unit": "1",
                    "quantity": str(quantity),
                    "source": "CAD_IDENTITY",
                }
                continue
            mark = fresh.get(key, {})
            try:
                rate = floor_value(Decimal(mark["cad_per_unit"]))
                value = stored(quantity * rate)
                if (
                    rate <= 0
                    or value <= 0
                    or not mark.get("received_at")
                    or Decimal(mark.get("quantity", "0")) != quantity
                ):
                    raise ValueError("Missing full-size initialization evidence")
                trial_marks[key] = {**mark, "cad_per_unit": str(rate)}
            except (KeyError, TypeError, ValueError, ArithmeticError):
                possible = False
                break
        if not possible:
            route["reasons"].append("FULL_SIZE_CAD_MARK_UNAVAILABLE")
            continue
        value = sum(
            (stored(q * Decimal(trial_marks[k]["cad_per_unit"])) for k, q in candidate.items()),
            Decimal(0),
        )
        if value > capital:
            route["reasons"].append("PAIRED_INVENTORY_EXCEEDS_INITIAL_CAPITAL")
            continue
        selected, marks = candidate, trial_marks
    allocations = []
    used = Decimal(0)
    for (ex, asset), quantity in sorted(selected.items()):
        mark = marks[(ex, asset)]
        rate = Decimal(mark["cad_per_unit"])
        value = stored(quantity * rate)
        used += value
        allocations.append(
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
                    "allocation_policy": "COMPLETE_ALTERNATIVE_ROUTES",
                },
            }
        )
    remainder = capital - used
    if remainder > 0:
        allocations.append(
            {
                "exchange": cash_exchange.slug,
                "asset": "fiat:CAD",
                "quantity": str(remainder),
                "cad_rate": "1",
                "cad_value": str(remainder),
                "evidence": {"source": "CAD_IDENTITY", "cash_remainder": True},
            }
        )
    return allocations


@decimal_context
def classify_funded_requirements(requirements, allocations):
    available = Counter()
    for holding in allocations:
        available[(holding["exchange"], holding["asset"])] += Decimal(holding["quantity"])
    for route in requirements:
        route["funded"] = bool(route.get("required")) and all(
            available[(item["exchange"], item["canonical_key"])]
            >= Decimal(item["required_quantity"])
            for item in route["required"]
        )
        if route["funded"]:
            route["planning_rejections_before_final_coverage"] = list(route["reasons"])
            route["reasons"] = []
        if not route["funded"] and not route["reasons"]:
            route["reasons"].append("INSUFFICIENT_PAIRED_ALLOCATION")
    return sum(x["funded"] for x in requirements)


def optimized_strategy(body: ProposalRequest) -> PaperStrategy:
    return presets()["Aggressive Research"].model_copy(
        update={
            "trade_amount_cad": body.trade_amount_cad,
            "min_buy_capital_cad": body.min_buy_capital_cad,
            "starting_capital_cad": body.capital_cad,
            "allowed_exchanges": body.exchange_slugs,
            "allowed_assets": body.asset_keys,
        }
    )


def constraints_for(body: ProposalRequest) -> allocation.AllocationConstraints:
    return allocation.AllocationConstraints(
        capital_cad=body.capital_cad,
        target_trade_cad=body.trade_amount_cad,
        min_buy_capital_cad=body.min_buy_capital_cad,
        min_cell_cad=body.min_cell_cad,
        max_venue_pct=body.max_venue_pct,
        max_asset_pct=body.max_asset_pct,
        max_reserve_pct=body.max_reserve_pct,
        max_stablecoin_pct=body.max_stablecoin_pct,
        min_venues=body.min_venues,
        reserve_pct=body.reserve_cash_pct,
    )


async def load_planning_rows(db, body, cutoff):
    chosen = list(
        await db.scalars(
            select(Exchange)
            .where(
                Exchange.enabled.is_(True),
                Exchange.created_at <= cutoff,
                Exchange.slug.in_(body.exchange_slugs) if body.exchange_slugs else True,
            )
            .order_by(Exchange.slug)
        )
    )
    if not chosen or set(body.exchange_slugs) - {x.slug for x in chosen}:
        raise HTTPException(422, "No matching supported exchange allocation at cutoff")
    ids = {x.id for x in chosen}
    buy, sell = aliased(Market), aliased(Market)
    buy_book, sell_book = aliased(OrderBookSnapshot), aliased(OrderBookSnapshot)
    conditions = [
        ArbitrageEvaluation.evaluated_at.between(
            cutoff - timedelta(days=body.lookback_days), cutoff
        ),
        ArbitrageEvaluation.created_at <= cutoff,
    ]
    positive_count = func.count(ArbitrageEvaluation.id).filter(
        ArbitrageEvaluation.net_profit_cad > 0
    )
    rows = (
        await db.execute(
            select(ArbitrageRoute, positive_count.label("positive"))
            .join(ArbitrageEvaluation)
            .join(buy, buy.id == ArbitrageRoute.buy_market_id)
            .join(sell, sell.id == ArbitrageRoute.sell_market_id)
            .join(buy_book, buy_book.id == ArbitrageEvaluation.buy_snapshot_id)
            .join(sell_book, sell_book.id == ArbitrageEvaluation.sell_snapshot_id)
            .where(
                *conditions,
                buy_book.quality.in_(["HEALTHY", "DEGRADED"]),
                sell_book.quality.in_(["HEALTHY", "DEGRADED"]),
                buy.created_at <= cutoff,
                sell.created_at <= cutoff,
                buy.exchange_id.in_(ids),
                sell.exchange_id.in_(ids),
                buy.base_asset_id.in_(
                    select(Asset.id).where(Asset.canonical_key.in_(body.asset_keys))
                )
                if body.asset_keys
                else True,
                buy.quote_asset_id.in_(
                    select(Asset.id).where(Asset.canonical_key.in_(body.inventory_asset_keys))
                )
                if body.inventory_asset_keys
                else True,
                sell.base_asset_id.in_(
                    select(Asset.id).where(Asset.canonical_key.in_(body.inventory_asset_keys))
                )
                if body.inventory_asset_keys
                else True,
            )
            .group_by(ArbitrageRoute.id)
            .order_by(
                positive_count.desc(), func.count(ArbitrageEvaluation.id).desc(), ArbitrageRoute.id
            )
            .limit(31)
        )
    ).all()
    selected = rows[:30]
    market_ids = {m for route, _ in selected for m in (route.buy_market_id, route.sell_market_id)}
    markets = {m.id: m for m in await db.scalars(select(Market).where(Market.id.in_(market_ids)))}
    exchanges, assets = await metadata(
        db, {item for m in markets.values() for item in (m.base_asset_id, m.quote_asset_id)}
    )
    cad = next((a for a in assets.values() if a.canonical_key == "fiat:CAD"), None)
    if cad is None:
        raise HTTPException(409, "CAD metadata not yet discovered")
    evaluations = {
        row.route_id: row
        for row in await db.scalars(
            select(ArbitrageEvaluation)
            .options(defer(ArbitrageEvaluation.result))
            .where(
                ArbitrageEvaluation.route_id.in_([r.id for r, _ in selected]),
                *conditions,
            )
            .distinct(ArbitrageEvaluation.route_id)
            .order_by(
                ArbitrageEvaluation.route_id,
                ArbitrageEvaluation.evaluated_at.desc(),
                ArbitrageEvaluation.id,
            )
        )
    }
    routes = []
    for route, frequency in selected:
        left, right = markets[route.buy_market_id], markets[route.sell_market_id]
        lx, rx = exchanges[left.exchange_id], exchanges[right.exchange_id]
        if left.base_asset_id != right.base_asset_id or venue_group(lx.slug) == venue_group(
            rx.slug
        ):
            continue
        quote, base = assets[left.quote_asset_id], assets[right.base_asset_id]
        if route.id in evaluations:
            routes.append(
                HistoricalRoute(
                    str(route.id),
                    allocation.Cell(lx.slug, quote.canonical_key),
                    allocation.Cell(rx.slug, base.canonical_key),
                    evaluations[route.id],
                    max(1, int(frequency)),
                    str(lx.id),
                    str(rx.id),
                    str(quote.id),
                    str(base.id),
                    quote.symbol,
                    base.symbol,
                    positive_frequency=int(frequency),
                )
            )
    observed_cad = set(
        await db.scalars(
            select(Market.exchange_id)
            .where(
                Market.quote_asset_id == cad.id,
                Market.exchange_id.in_(ids),
                Market.created_at <= cutoff,
            )
            .distinct()
        )
    )
    reserve_venues = [
        x.slug
        for x in chosen
        if x.id in observed_cad
        and (not body.inventory_asset_keys or cad.canonical_key in body.inventory_asset_keys)
    ]
    cash_exchange = next(
        (
            x
            for x in sorted(chosen, key=lambda x: (x.slug not in {"kraken", "ndax"}, x.slug))
            if x.id in observed_cad
        ),
        chosen[0],
    )
    return routes, exchanges, assets, reserve_venues, cash_exchange, len(rows) > 30


def proposal_marks_fresh(payload, now, maximum_age_seconds=30):
    for item in payload["allocations"]:
        if item["asset"] == allocation.CAD:
            continue
        evidence = item.get("evidence", {})
        mark = evidence.get("mark", evidence)
        try:
            received, observed = time_value(mark["received_at"]), time_value(mark["observed_at"])
            if not (now - timedelta(seconds=maximum_age_seconds) <= observed <= received <= now):
                return False
        except (TypeError, ValueError, KeyError):
            return False
    return True


async def baseline_comparison(
    body, cohort, constraints, exchanges, assets, cash_exchange, venue_groups, stablecoins, cutoff
):
    investable = floor_value(body.capital_cad * (1 - body.reserve_cash_pct / 100))

    async def source(items):
        return {
            (row["exchange_id"], row["asset_id"]): cohort.legacy_marks.get(
                (row["exchange_id"], row["asset_id"], Decimal(row["quantity"])), {}
            )
            for row in items
        }

    contributions = await allocate_complete_routes(
        investable, cohort.legacy_requirements, source, exchanges, assets, cash_exchange
    )
    reserve = body.capital_cad - investable
    if reserve:
        contributions.append(
            {
                "exchange": cash_exchange.slug,
                "asset": allocation.CAD,
                "quantity": str(reserve),
                "cad_rate": "1",
                "cad_value": str(reserve),
                "evidence": {"source": "CAD_IDENTITY", "reserve": True},
            }
        )
    legacy_count = classify_funded_requirements(cohort.legacy_requirements, contributions)
    quantities, by_venue, by_asset = Counter(), Counter(), Counter()
    idle = Decimal(0)
    for item in contributions:
        cad_value = Decimal(item["cad_value"])
        by_venue[item["exchange"]] += cad_value
        by_asset[item["asset"]] += cad_value
        if item["evidence"].get("reserve") or item["evidence"].get("cash_remainder"):
            idle += cad_value
        else:
            quantities[allocation.Cell(item["exchange"], item["asset"])] += Decimal(
                item["quantity"]
            )

    def breakdown(values):
        return {
            key: {"cad_value": str(value), "share_pct": str(stored(value / body.capital_cad * 100))}
            for key, value in sorted(values.items())
        }

    stable = sum((by_asset[key] for key in stablecoins), Decimal(0))
    assessment = allocation.describe_inventory(
        constraints,
        cohort.demands,
        quantities,
        venue_groups=venue_groups,
        stablecoin_assets=stablecoins,
        as_of=cutoff,
    )
    return {
        **assessment,
        "policy": "COVERAGE",
        "feasible": False,
        "allocations": contributions,
        "total_cad": str(sum(by_asset.values(), Decimal(0))),
        "unallocated_cad": "0",
        "legacy_individual_coverage": legacy_count,
        "reasons": ["BASELINE_REPRODUCTION_FOR_COMPARISON"],
        "allocation_summary": {
            "invested_cad": str(body.capital_cad - idle),
            "reserve_cad": str(idle),
            "reserve_pct": str(stored(idle / body.capital_cad * 100)),
            "by_exchange": breakdown(by_venue),
            "by_asset": breakdown(by_asset),
            "funded_cells": len(quantities),
            "funded_venues": len({venue_groups.get(c.exchange, c.exchange) for c in quantities}),
            "stablecoin_cad": str(stable),
            "stablecoin_pct": str(stored(stable / body.capital_cad * 100)),
        },
    }


@decimal_context
async def build_proposals(body, request, db, user, policies):
    if body.eligibility != "RESEARCH":
        raise HTTPException(
            422,
            "Validated allocation needs verified account eligibility and fees",
        )
    if PLANNING_LIMIT.locked():
        raise HTTPException(429, "Another bounded capital plan is running")
    async with PLANNING_LIMIT:
        now = await now_checked(request)
        cutoff = body.planning_cutoff or now
        if cutoff > now:
            raise HTTPException(422, "Planning cutoff cannot be in the future")
        await db.scalar(select(User).where(User.id == user.id).with_for_update())
        recent = await db.scalar(
            select(func.count())
            .select_from(FundingProposal)
            .where(
                FundingProposal.user_id == user.id,
                FundingProposal.expires_at >= now - timedelta(minutes=55),
            )
        )
        if recent + len(policies) > 30:
            raise HTTPException(429, "Funding proposal limit reached; at most30 per hour")
        (
            routes,
            exchanges,
            assets,
            reserve_venues,
            cash_exchange,
            limited,
        ) = await load_planning_rows(db, body, cutoff)
        strategy = optimized_strategy(body)
        constraints = constraints_for(body)
        groups = {exchange.slug: venue_group(exchange.slug) for exchange in exchanges.values()}
        stablecoins = {
            asset.canonical_key for asset in assets.values() if asset.kind == "stablecoin"
        }
        cohort = await asyncio.to_thread(
            funding_demands, routes, cutoff, strategy, include_legacy="COVERAGE" in policies
        )
        outputs = []
        for policy in policies:
            if policy == "COVERAGE":
                data = await baseline_comparison(
                    body,
                    cohort,
                    constraints,
                    exchanges,
                    assets,
                    cash_exchange,
                    groups,
                    stablecoins,
                    cutoff,
                )
            else:
                plan = await asyncio.to_thread(
                    allocation.optimize_allocation,
                    policy,
                    constraints,
                    cohort.demands,
                    reserve_venues,
                    stablecoins,
                    venue_groups=groups,
                    as_of=cutoff,
                )
                data = plan.as_payload()
            details = {item["route_id"]: item for item in data["route_capacity"]}
            requirements = [
                {
                    **row,
                    "funded": bool(
                        (details.get(row["route_id"], {}).get("target_pinned") or {}).get(
                            "target_met"
                        )
                    )
                    and "INSUFFICIENT_PRE_FUNDED_INVENTORY"
                    not in details[row["route_id"]]["limiting_constraints"],
                }
                for row in cohort.requirements
            ]
            ready = (
                data["feasible"]
                and body.planning_cutoff is None
                and proposal_marks_fresh(data, now, strategy.maximum_book_age_seconds)
            )
            planned_strategy = strategy
            if policy != "COVERAGE" and data["allocations"]:
                active = [
                    item for item in data["allocations"] if not item["evidence"].get("reserve")
                ]
                # Restrict this experiment to its modeled inventory universe. Preserve
                # existing risk settings and earlier strategy versions.
                planned_strategy = strategy.model_copy(
                    update={
                        "allowed_exchanges": tuple(sorted({item["exchange"] for item in active})),
                        "allowed_assets": tuple(
                            sorted(
                                {
                                    item["asset"]
                                    for item in active
                                    if (item["asset"] in {route.sell.asset for route in routes})
                                }
                            )
                        ),
                    }
                )
            payload = {
                **data,
                "allocation_version": allocation.VERSION,
                "classification": "RESEARCH",
                "as_of": cutoff.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "capital_cad": str(body.capital_cad),
                "trade_amount_cad": str(body.trade_amount_cad),
                "requirements": requirements,
                "creatable": ready,
                "eligibility_verified": False,
                "creation_requires_fresh_marks": True,
                "proposed_strategy": planned_strategy.model_dump(mode="json"),
                "planning_window": {
                    "from": (cutoff - timedelta(days=body.lookback_days)).isoformat(),
                    "cutoff": cutoff.isoformat(),
                    "historical": body.planning_cutoff is not None,
                },
                "evidence": cohort.records,
                "evidence_exclusions": dict(cohort.exclusions),
                "route_coverage": {
                    "considered_routes": len(routes),
                    "route_window_limit": 30,
                    "route_window_limited": limited,
                    "funded_route_count": data["funded_route_count"],
                },
                "limitations": [
                    "Retained research projections use explicit source cutoffs and costs",
                    "Largest funded size refers to evaluated size points",
                    "Funding coverage excludes idle reserve and cannot establish actionability",
                    "Historical or stale priced positions cannot initialize a current portfolio",
                    "Validated account eligibility and fees remain unverified",
                ],
            }
            row = FundingProposal(
                user_id=user.id, expires_at=now + timedelta(minutes=5), payload=payload
            )
            db.add(row)
            await db.flush()
            outputs.append({"id": str(row.id), **payload})
        await db.commit()
        return {"as_of": cutoff.isoformat(), "proposals": outputs}


@router.post("/compare")
async def compare(body: ProposalRequest, request: Request, db: DB, user: AuthenticatedUser):
    return await build_proposals(body, request, db, user, ("COVERAGE", *OPTIMIZED_POLICIES))


@router.post("/proposal")
@decimal_context
async def proposal(body: ProposalRequest, request: Request, db: DB, user: AuthenticatedUser):
    if body.policy in OPTIMIZED_POLICIES:
        return (await build_proposals(body, request, db, user, (body.policy,)))["proposals"][0]
    if (
        body.eligibility != "RESEARCH"
        or body.planning_cutoff is not None
        or body.inventory_asset_keys
    ):
        raise HTTPException(422, "Use a Stage 8B policy or comparison for these constraints")
    now = await now_checked(request)
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    recent = await db.scalar(
        select(func.count())
        .select_from(FundingProposal)
        .where(
            FundingProposal.user_id == user.id,
            FundingProposal.expires_at >= now - timedelta(minutes=55),
        )
    )
    if recent >= 30:
        raise HTTPException(429, "Funding proposal limit reached; at most30per hour")
    exchanges = {x.id: x for x in await db.scalars(select(Exchange))}
    chosen = [
        x
        for x in exchanges.values()
        if x.enabled and (not body.exchange_slugs or x.slug in body.exchange_slugs)
    ]
    if not chosen or set(body.exchange_slugs) - {x.slug for x in chosen}:
        raise HTTPException(422, "No matching supported exchange allocation")
    chosen.sort(key=lambda x: x.slug)
    chosen_ids = {x.id for x in chosen}
    buy_listing, sell_listing = aliased(Market), aliased(Market)
    counts = list(
        (
            await db.execute(
                select(ArbitrageRoute, func.count(ArbitrageEvaluation.id))
                .join(ArbitrageEvaluation)
                .join(buy_listing, buy_listing.id == ArbitrageRoute.buy_market_id)
                .join(sell_listing, sell_listing.id == ArbitrageRoute.sell_market_id)
                .where(
                    buy_listing.exchange_id.in_(chosen_ids),
                    sell_listing.exchange_id.in_(chosen_ids),
                    buy_listing.base_asset_id.in_(
                        select(Asset.id).where(Asset.canonical_key.in_(body.asset_keys))
                    )
                    if body.asset_keys
                    else True,
                    ArbitrageEvaluation.evaluated_at >= now - timedelta(days=body.lookback_days),
                    ArbitrageEvaluation.evaluated_at <= now,
                    ArbitrageEvaluation.net_profit_cad > 0,
                )
                .group_by(ArbitrageRoute.id)
                .order_by(func.count(ArbitrageEvaluation.id).desc(), ArbitrageRoute.id)
                .limit(31)
            )
        ).all()
    )
    mids = {mid for r, _ in counts for mid in (r.buy_market_id, r.sell_market_id)}
    markets = {x.id: x for x in await db.scalars(select(Market).where(Market.id.in_(mids)))}
    exchanges, assets = await metadata(
        db, {a for m in markets.values() for a in (m.base_asset_id, m.quote_asset_id)}
    )
    cad = next((a for a in assets.values() if a.canonical_key == "fiat:CAD"), None)
    if cad is None:
        raise HTTPException(409, "CAD metadata not yet discovered")
    latest = list(
        await db.scalars(
            select(ArbitrageEvaluation)
            .where(
                ArbitrageEvaluation.route_id.in_([r.id for r, _ in counts]),
                ArbitrageEvaluation.evaluated_at <= now,
            )
            .distinct(ArbitrageEvaluation.route_id)
            .order_by(
                ArbitrageEvaluation.route_id,
                ArbitrageEvaluation.evaluated_at.desc(),
                ArbitrageEvaluation.id,
            )
        )
    )
    evaluations = {x.route_id: x for x in latest}
    strategy = presets()["Aggressive Research"].model_copy(
        update={"trade_amount_cad": body.trade_amount_cad}
    )
    weights, covered, requirements = Counter(), 0, []
    for route, n in counts[:30]:
        buy, sell = markets[route.buy_market_id], markets[route.sell_market_id]
        if buy.exchange_id not in chosen_ids or sell.exchange_id not in chosen_ids:
            continue
        if body.asset_keys and assets[buy.base_asset_id].canonical_key not in body.asset_keys:
            continue
        covered += 1
        evaluation = evaluations.get(route.id)
        item = {
            "route_id": str(route.id),
            "evaluation_id": str(evaluation.id) if evaluation else None,
            "asset": assets[buy.base_asset_id].symbol,
            "buy_exchange": exchanges[buy.exchange_id].slug,
            "sell_exchange": exchanges[sell.exchange_id].slug,
            "trade_amount_cad": str(body.trade_amount_cad),
            "required": [],
            "reasons": [],
            "funded": False,
            "actionable": False,
            "classification": "RESEARCH",
        }
        try:
            if (
                not evaluation
                or buy.base_asset_id != sell.base_asset_id
                or venue_group(exchanges[buy.exchange_id].slug)
                == venue_group(exchanges[sell.exchange_id].slug)
            ):
                raise ValueError("COMPATIBLE_PINNED_INPUTS_UNAVAILABLE")
            best, quote_quantity, base_quantity = await asyncio.to_thread(
                required_quantities, evaluation, strategy
            )
            item["evaluated_at"] = evaluation.evaluated_at.isoformat()
            item["projected_net_profit_cad"] = (
                str(best.net_profit_cad) if best.net_profit_cad is not None else None
            )
            for m, asset_id, quantity in (
                (buy, buy.quote_asset_id, quote_quantity),
                (sell, sell.base_asset_id, base_quantity),
            ):
                item["required"].append(
                    {
                        "exchange_id": str(m.exchange_id),
                        "exchange": exchanges[m.exchange_id].slug,
                        "asset_id": str(asset_id),
                        "asset": assets[asset_id].symbol,
                        "canonical_key": assets[asset_id].canonical_key,
                        "required_quantity": str(quantity),
                    }
                )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            item["required"] = []
            item["reasons"].append("COMPATIBLE_PINNED_SIZE_UNAVAILABLE")
        requirements.append(item)
        weight = Decimal(n) if body.policy == "COVERAGE" else Decimal(1)
        weights[(str(buy.exchange_id), str(buy.quote_asset_id))] += weight
        weights[(str(sell.exchange_id), str(sell.base_asset_id))] += weight
    # Equal venues normalizes each venue's covered asset demand to the same budget.
    if body.policy == "EQUAL_VENUES":
        totals = Counter()
        for (ex, _), weight in weights.items():
            totals[ex] += weight
        weights = Counter({key: weight / totals[key[0]] for key, weight in weights.items()})
    limits = []
    if body.policy == "EQUAL_VENUES" and len(weights) > 40:
        weights = Counter(dict(weights.most_common(40)))
        limits.append("Only40highest-coverage venue/asset allocation cells included")
    weight_total = sum(weights.values(), Decimal(0))
    investable = floor_value(body.capital_cad * (1 - body.reserve_cash_pct / 100))
    cad_venues = set(
        await db.scalars(
            select(Market.exchange_id)
            .where(
                Market.quote_asset_id == cad.id,
                Market.active.is_not(False),
                Market.exchange_id.in_(chosen_ids),
            )
            .distinct()
        )
    )
    cash_exchange = next(
        (
            x
            for x in sorted(chosen, key=lambda x: (x.slug not in {"kraken", "ndax"}, x.slug))
            if x.id in cad_venues
        ),
        chosen[0],
    )
    if not cad_venues:
        limits.append(
            "Reserve CAD is accounting inventory; selected venues have no discovered CAD market"
        )
    if body.policy == "COVERAGE":

        async def full_size_source(items):
            return await quote_marks(request, items)

        allocations = await allocate_complete_routes(
            investable, requirements, full_size_source, exchanges, assets, cash_exchange
        )
    else:
        quantities = [
            {"exchange_id": ex, "asset_id": asset, "quantity": "0.000001"}
            for ex, asset in weights
            if asset != str(cad.id)
        ]
        probe = await quote_marks(request, quantities)
        rates = {(str(x.id), str(cad.id)): Decimal(1) for x in chosen}
        for key, value in probe.items():
            try:
                if value.get("cad_per_unit") is not None:
                    rates[key] = floor_value(Decimal(value["cad_per_unit"]))
            except (ValueError, TypeError, ArithmeticError):
                continue
        full_requests = []
        for (ex, asset), weight in weights.items():
            if asset == str(cad.id) or weight_total <= 0:
                continue
            quantity = quantity_for_budget(
                investable * weight / weight_total, rates.get((ex, asset), Decimal(0))
            )
            if quantity is not None:
                full_requests.append(
                    {"exchange_id": ex, "asset_id": asset, "quantity": str(quantity)}
                )
        full = await quote_marks(request, full_requests)
        allocations = await allocate_weighted(
            investable, weights, rates, full, exchanges, assets, cash_exchange
        )
    cash_reserve = body.capital_cad - investable
    if cash_reserve:
        allocations.append(
            {
                "exchange": cash_exchange.slug,
                "asset": "fiat:CAD",
                "quantity": str(cash_reserve),
                "cad_rate": "1",
                "cad_value": str(cash_reserve),
                "evidence": {"source": "CAD_IDENTITY", "reserve": True},
            }
        )
    if not covered:
        limits.append("No positive retained route coverage; proposal keeps capital in CAD")
    if body.policy == "EQUAL_VENUES" and any(
        key not in rates or (key[1] != str(cad.id) and key not in full) for key in weights
    ):
        limits.append(
            "Missing executable-depth CAD marks retained as cash; no stablecoin parity assumed"
        )
    funded_count = classify_funded_requirements(requirements, allocations)
    if not funded_count and covered:
        limits.append(
            "No complete route is funded at the selected trade budget; inspect paired deficits"
        )
    now = await now_checked(request)
    payload = {
        "classification": "RESEARCH",
        "policy": body.policy,
        "capital_cad": str(body.capital_cad),
        "trade_amount_cad": str(body.trade_amount_cad),
        "funded_route_count": funded_count,
        "requirements": requirements,
        "allocations": allocations,
        "total_cad": str(sum(Decimal(a["cad_value"]) for a in allocations)),
        "unallocated_cad": "0",
        "route_coverage": {
            "considered_routes": covered,
            "historical_window_days": body.lookback_days,
            "route_window_limit": 30,
            "route_window_limited": len(counts) > 30,
            "funded_route_count": funded_count,
        },
        "limitations": [
            *limits,
            "Hypothetical initial contributions, not actual purchases",
            "Historical coverage is not a forecast of profitability",
            "Shared cells cover alternative routes individually, not simultaneous reservations",
            "Pinned projections can expire or change before a new candidate or reaction delay",
            "Funding alone does not approve exposure, loss, confidence or concurrent strategy risk",
            "Fees, trading eligibility and inventory assumptions remain unverified",
        ],
        "as_of": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    row = FundingProposal(user_id=user.id, expires_at=now + timedelta(minutes=5), payload=payload)
    db.add(row)
    await db.commit()
    return {"id": str(row.id), **payload}


class ExperimentRequest(Contract):
    name: str = Field(default="Research allocation experiment", min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=100)
    proposal_id: UUID | None = None
    capital_cad: PositiveDecimal = "10000"
    allocations: tuple[Allocation, ...] | None = None
    settings: PaperStrategy | None = None
    reset_from_session_id: UUID | None = None

    @model_validator(mode="after")
    def source(self):
        if bool(self.proposal_id) == bool(self.allocations):
            raise ValueError("Use one funding source: proposal or explicit allocations")
        return self


@router.post("/experiments", status_code=201)
async def experiment(body: ExperimentRequest, request: Request, db: DB, user: AuthenticatedUser):
    now = await now_checked(request)
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    proposal_row = await db.get(FundingProposal, body.proposal_id) if body.proposal_id else None
    if body.proposal_id and (proposal_row is None or proposal_row.user_id != user.id):
        raise HTTPException(404, "Funding proposal not found")
    optimized = bool(proposal_row and proposal_row.payload.get("allocation_version"))
    if not optimized and body.idempotency_key.startswith("allocation:v2:"):
        raise HTTPException(422, "Reserved allocation idempotency namespace")
    key = f"allocation:v2:{body.proposal_id}" if optimized else body.idempotency_key
    fingerprint = body.model_dump(mode="json")
    if optimized:
        fingerprint["idempotency_key"] = key
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prior = await db.scalar(
        select(FundingExperiment).where(
            FundingExperiment.user_id == user.id,
            FundingExperiment.idempotency_key == key,
        )
    )
    if prior:
        if prior.fingerprint != digest:
            raise HTTPException(409, "Idempotency key was used for a different experiment")
        session, control = await owned(db, user, prior.session_id)
        return {**session_dto(session, control), "reused": True}
    if body.reset_from_session_id:
        source, control = await owned(db, user, body.reset_from_session_id)
        if control.classification != "RESEARCH":
            raise HTTPException(409, "Validated portfolios cannot be reset as research")
        if source.initial_equity_cad != body.capital_cad:
            raise HTTPException(422, "Reset comparison must use equal initial capital")
    allocations = list(body.allocations or [])
    if proposal_row is not None:
        p = proposal_row
        if p.expires_at < now:
            raise HTTPException(409, "Funding proposal expired; request fresh marks")
        if Decimal(p.payload["capital_cad"]) != body.capital_cad:
            raise HTTPException(422, "Experiment capital differs from its proposal")
        if optimized and (
            p.payload.get("creatable") is not True
            or p.payload.get("feasible") is not True
            or not proposal_marks_fresh(p.payload, now)
        ):
            raise HTTPException(409, "Select a feasible current allocation with fresh CAD marks")
        allocations = [
            Allocation.model_validate(
                {
                    **{k: v for k, v in item.items() if k not in {"cad_value", "evidence"}},
                    "evidence": {
                        **item.get("evidence", {}),
                        "funding_proposal_id": str(p.id),
                        "allocation_version": p.payload.get("allocation_version"),
                        "proposal_trade_amount_cad": p.payload.get("trade_amount_cad", "250"),
                        "proposal_funded_alternative_routes": p.payload.get(
                            "funded_route_count", 0
                        ),
                    },
                }
            )
            for item in p.payload["allocations"]
        ]
    settings = body.settings or presets()["Aggressive Research"]
    if optimized:
        pinned = PaperStrategy.model_validate(proposal_row.payload["proposed_strategy"])
        if body.settings and body.settings != pinned:
            raise HTTPException(422, "Use the selected proposal's immutable strategy settings")
        settings = pinned
    elif proposal_row is not None:
        target = Decimal(proposal_row.payload.get("trade_amount_cad", "250"))
        if body.settings and settings.trade_amount_cad != target:
            raise HTTPException(422, "Strategy trade budget must match its funding proposal")
        settings = settings.model_copy(update={"trade_amount_cad": target})
    if settings.mode != "RESEARCH_SHADOW":
        raise HTTPException(422, "Funding experiments require isolated RESEARCH_SHADOW mode")
    settings = settings.model_copy(update={"starting_capital_cad": body.capital_cad})
    try:
        session, control = await create_portfolio(
            db, user.id, body.name, settings, body.capital_cad, allocations, now
        )
        db.add(
            FundingExperiment(
                user_id=user.id,
                session_id=session.id,
                idempotency_key=key,
                fingerprint=digest,
                source_session_id=body.reset_from_session_id,
            )
        )
        await db.commit()
        return {**session_dto(session, control), "reused": False}
    except (AccountingError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from None
