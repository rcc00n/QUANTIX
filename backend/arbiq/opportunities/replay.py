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


def replay_document(
    document: dict,
    *,
    at: datetime | None = None,
    budget_cad: Decimal | None = None,
) -> dict:
    """Read explicit fixture frames or the authenticated retained opportunity detail."""
    if document.get("schema_version") == "quantix-replay-v1":
        if document.get("kind") not in {"SYNTHETIC", "RETAINED_EVIDENCE"}:
            raise ValueError(
                "Replay documents must explicitly identify SYNTHETIC or RETAINED_EVIDENCE"
            )
        synthetic = document["kind"] == "SYNTHETIC"
        if len(document.get("books", [])) > 10000:
            raise ValueError("Replay documents are limited to 10000 book records")
        records = tuple(
            ReplayBook(
                load_input(record["market"]),
                datetime.fromisoformat(record["observed_at"]),
                datetime.fromisoformat(record["received_at"]),
                record["evidence_id"],
                synthetic,
            )
            for record in document["books"]
        )
        decisions = (at,) if at else tuple(datetime.fromisoformat(x) for x in document["decisions"])
        buy_id, sell_id = document["buy_market_id"], document["sell_market_id"]
        budget = budget_cad if budget_cad is not None else Decimal(document["budget_cad"])
        costs = load_costs(document["costs"]) if "costs" in document else DEFAULT_COSTS
        kind = document["kind"]
        retained_warnings = []
        max_search_evaluations = 4096
    else:
        evidence = document.get("evidence", {})
        inputs = evidence.get("calculation_inputs")
        if not inputs:
            raise ValueError("No retained calculation inputs; supply a saved opportunity detail")
        original_decision = datetime.fromisoformat(inputs["evaluated_at"])
        if at is not None and at < original_decision:
            # Fee/config availability before the original decision is not established
            # by older book timestamps. Do not backdate that bundled evidence.
            raise ValueError(
                "Retained calculation assumptions are unavailable before their original decision"
            )
        records_list = []
        retained_warnings = list(document.get("reasons", []))
        for side in ("buy", "sell"):
            book = evidence.get(f"{side}_book", {})
            observed = book.get("exchange_timestamp") or book.get("normalized", {}).get(
                "observed_at"
            )
            received = book.get("received_at")
            if not observed or not received:
                raise ValueError(
                    "Retained replay requires observed/received book evidence; "
                    "live projections without pinned books are insufficient"
                )
            records_list.append(
                ReplayBook(
                    load_input(inputs[side]),
                    datetime.fromisoformat(observed),
                    datetime.fromisoformat(received),
                    evidence.get(f"{side}_snapshot_id") or book.get("content_sha256") or "",
                    document.get("synthetic") is True,
                )
            )
        records = tuple(records_list)
        decisions = (at or original_decision,)
        buy_id, sell_id = inputs["buy"]["market_id"], inputs["sell"]["market_id"]
        budget = budget_cad if budget_cad is not None else Decimal(document["selected_budget_cad"])
        costs = load_costs(inputs["costs"])
        max_search_evaluations = inputs.get("max_search_evaluations", 4096)
        kind = "RETAINED_EVIDENCE"
    if len(decisions) > 1000:
        raise ValueError("Replay documents are limited to 1000 decision times")
    decisions_result = replay_route(
        records,
        buy_market_id=buy_id,
        sell_market_id=sell_id,
        decision_times=decisions,
        budget_cad=budget,
        costs=costs,
        max_search_evaluations=max_search_evaluations,
    )
    return safe_json(
        {
            "schema_version": "quantix-replay-result-v1",
            "kind": kind,
            "paper_only": True,
            "actionable": False,
            "budget_cad": budget,
            "max_search_evaluations": max_search_evaluations,
            "scope": (
                "Deterministic projections from supplied retained evidence; "
                "no execution or live verification"
            ),
            "retained_risk_warnings": retained_warnings,
            "decisions": [asdict(result) for result in decisions_result],
        }
    )


