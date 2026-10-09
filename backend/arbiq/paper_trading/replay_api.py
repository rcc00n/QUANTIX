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

router = APIRouter(tags=["causal research replay"])


class ReplayRequest(Contract):
    evaluation_id: UUID
    reaction_delay_seconds: int = Field(default=30, ge=0, le=3600)
    budget_cad: PositiveDecimal = Field(default="250", le=1000000)
    classification: Literal["RESEARCH"] = "RESEARCH"


@router.post("/paper/replay")
async def historical_replay(body: ReplayRequest, request: Request, db: DB, user: AuthenticatedUser):
    gate = getattr(request.app.state, "paper_replay_gate", None)
    if gate is None:
        gate = request.app.state.paper_replay_gate = asyncio.Semaphore(2)
    if gate.locked():
        raise HTTPException(429, "Two replay jobs are already running; retry after they complete")
    async with gate:
        now = await now_checked(request)
        return await replay_evidence(body, db, now)


