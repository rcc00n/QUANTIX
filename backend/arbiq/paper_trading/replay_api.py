"""Causal retained-evidence research replay, never virtual fills or ledger mutation."""

import asyncio
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from sqlalchemy import select

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import ArbitrageEvaluation, ArbitrageRoute, OrderBookSnapshot
from arbiq.finance.contracts import Contract, PositiveDecimal
from arbiq.market_data.contracts import BookLevel
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.analysis import load_costs, load_input
from arbiq.opportunities.replay import ReplayBook, replay_document, replay_route
from arbiq.paper_trading.api import now_checked
from arbiq.paper_trading.replay_stress import compare_delays

router = APIRouter(tags=["causal research replay"])


class ReplayRequest(Contract):
    evaluation_id: UUID
    reaction_delay_seconds: int = Field(default=30, ge=0, le=3600)
    budget_cad: PositiveDecimal = Field(default="250", le=1000000)
    classification: Literal["RESEARCH"] = "RESEARCH"
    include_delay_matrix: bool = False


@router.post("/paper/replay")
async def historical_replay(body: ReplayRequest, request: Request, db: DB, user: AuthenticatedUser):
    gate = getattr(request.app.state, "paper_replay_gate", None)
    if gate is None:
        gate = request.app.state.paper_replay_gate = asyncio.Semaphore(2)
    if gate.locked():
        raise HTTPException(429, "Two replay jobs are already running; retry after they complete")
    async with gate:
        now = await now_checked(request)
        if body.include_delay_matrix:
            evaluation = await db.get(ArbitrageEvaluation, body.evaluation_id)
            if evaluation is None:
                raise HTTPException(404, "Retained evaluation not found")
            return await compare_delays(db, evaluation, now, body.budget_cad)
        return await replay_evidence(body, db, now)


async def replay_evidence(body, db, now):
    evaluation = await db.get(ArbitrageEvaluation, body.evaluation_id)
    if not evaluation:
        raise HTTPException(404, "Retained evaluation not found")
    route = await db.get(ArbitrageRoute, evaluation.route_id)
    document = {**evaluation.result, "evidence": evaluation.evidence}
    response = {
        "schema_version": "paper-research-replay-v1",
        "kind": "RETAINED_EVIDENCE",
        "classification": "RESEARCH",
        "paper_only": True,
        "actionable": False,
        "evaluation_id": str(evaluation.id),
        "detection_at": evaluation.evaluated_at.isoformat(),
        "reaction_delay_seconds": body.reaction_delay_seconds,
        "budget_cad": str(body.budget_cad),
        "original_projection": None,
        "delayed_projection": None,
        "limitations": [
            "Read-only counterfactual projections; no simulated orders/fills or portfolio changes",
            "Sparse retained snapshots cannot prove the first live post-delay observation",
            "No future data is available before its original ingestion timestamp",
        ],
    }
    try:
        response["original_projection"] = await asyncio.to_thread(
            replay_document, document, budget_cad=body.budget_cad
        )
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        response["limitations"].append("ORIGINAL_EVIDENCE_INSUFFICIENT: " + str(exc))
    due = evaluation.evaluated_at + timedelta(seconds=body.reaction_delay_seconds)
    records = []
    times = []
    try:
        inputs = evaluation.evidence["calculation_inputs"]
        if due > now:
            raise ValueError("DELAYED_EVIDENCE_NOT_YET_AVAILABLE")
        for side, market_id in (("buy", route.buy_market_id), ("sell", route.sell_market_id)):
            snapshot = await db.scalar(
                select(OrderBookSnapshot)
                .where(
                    OrderBookSnapshot.market_id == market_id,
                    OrderBookSnapshot.received_at >= due,
                    OrderBookSnapshot.received_at <= due + timedelta(seconds=60),
                    OrderBookSnapshot.received_at <= now,
                    OrderBookSnapshot.observed_at <= OrderBookSnapshot.received_at,
                    OrderBookSnapshot.quality.in_(["HEALTHY", "DEGRADED"]),
                )
                .order_by(OrderBookSnapshot.received_at, OrderBookSnapshot.id)
                .limit(1)
            )
            if snapshot is None:
                raise ValueError("DELAYED_BOOK_EVIDENCE_INSUFFICIENT")
            market = replace(
                load_input(inputs[side]),
                asks=tuple(BookLevel(price=p, quantity=q) for p, q in snapshot.asks),
                bids=tuple(BookLevel(price=p, quantity=q) for p, q in snapshot.bids),
            )
            records.append(
                ReplayBook(market, snapshot.observed_at, snapshot.received_at, str(snapshot.id))
            )
            times.append(snapshot.received_at)
        decision = max(times)
        result = await asyncio.to_thread(
            replay_route,
            tuple(records),
            buy_market_id=str(route.buy_market_id),
            sell_market_id=str(route.sell_market_id),
            decision_times=(decision,),
            budget_cad=body.budget_cad,
            costs=load_costs(inputs["costs"]),
            max_age=timedelta(seconds=30),
            max_search_evaluations=128,
        )
        response["delayed_projection"] = safe_json(
            {"eligible_at": due, "decision_at": decision, "decisions": [asdict(x) for x in result]}
        )
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        response["limitations"].append(str(exc))
    return response
