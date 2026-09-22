"""Deterministic retained-evidence replay; availability time prevents look-ahead."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from arbiq.finance.contracts import utc_timestamp
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.analysis import load_costs, load_input
from arbiq.opportunities.contracts import Costs, MarketInput, OptimizationResult
from arbiq.opportunities.math import DEFAULT_COSTS, optimize_route


@dataclass(frozen=True, slots=True)
class ReplayBook:
    market: MarketInput
    observed_at: datetime
    received_at: datetime
    evidence_id: str
    synthetic: bool = False

    def __post_init__(self) -> None:
        utc_timestamp(self.observed_at)
        utc_timestamp(self.received_at)
        if not self.evidence_id:
            raise ValueError("Replay requires provenance")


@dataclass(frozen=True, slots=True)
class ReplayDecision:
    decision_at: datetime
    evidence_ids: tuple[str, ...]
    result: OptimizationResult | None
    reason: str | None
    synthetic: bool


def replay_route(
    records: tuple[ReplayBook, ...],
    *,
    buy_market_id: str,
    sell_market_id: str,
    decision_times: tuple[datetime, ...],
    budget_cad: Decimal,
    costs: Costs = DEFAULT_COSTS,
    max_age: timedelta = timedelta(seconds=60),
    max_search_evaluations: int = 4096,
) -> tuple[ReplayDecision, ...]:
    if max_age <= timedelta(0):
        raise ValueError("Replay freshness must be positive")
    output = []
    for decision in sorted(decision_times):
        utc_timestamp(decision)
        selected: list[ReplayBook] = []
        for market_id in (buy_market_id, sell_market_id):
            available = [
                record
                for record in records
                if record.market.market_id == market_id
                and record.received_at <= decision
                and record.observed_at <= decision
            ]
            if available:
                selected.append(max(available, key=lambda x: (x.received_at, x.evidence_id)))
        reason = None
        result = None
        if len(selected) < 2:
            reason = "EVIDENCE_NOT_YET_AVAILABLE"
        elif any(
            decision - record.observed_at > max_age or decision - record.received_at > max_age
            for record in selected
        ):
            reason = "REPLAY_EVIDENCE_STALE"
        elif any(
            record.market.fx
            and record.market.fx.currency != "CAD"
            and (record.market.fx.available_at is None or record.market.fx.observed_at is None)
            for record in selected
        ):
            reason = "REPLAY_FX_PROVENANCE_INCOMPLETE"
        elif any(
            record.market.fx
            and any(
                timestamp is not None and timestamp > decision
                for timestamp in (record.market.fx.observed_at, record.market.fx.available_at)
            )
            for record in selected
        ):
            reason = "REPLAY_FX_EVIDENCE_NOT_YET_AVAILABLE"
        else:
            try:
                result = optimize_route(
                    selected[0].market,
                    selected[1].market,
                    budget_cad=budget_cad,
                    now=decision,
                    costs=costs,
                    budgets=(),
                    max_search_evaluations=max_search_evaluations,
                )
            except ValueError as error:
                reason = str(error)
        output.append(
            ReplayDecision(
                decision,
                tuple(x.evidence_id for x in selected),
                result,
                reason,
                any(x.synthetic for x in selected),
            )
        )
    return tuple(output)


