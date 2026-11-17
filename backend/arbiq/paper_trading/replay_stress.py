"""Bounded execution-delay scenarios from point-in-time route revisions."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from arbiq.db.models import ArbitrageEvaluation
from arbiq.finance.contracts import utc_timestamp
from arbiq.opportunities.replay import replay_document

DELAY_SECONDS = (0, 5, 15, 30)
RETAINED_WINDOW = timedelta(seconds=60)
MAX_SOURCE_AGE = timedelta(seconds=30)
MAX_SKEW = timedelta(seconds=10)
MAX_REVISIONS = 32
MAX_SEARCH = 128


def _timestamp(value):
    try:
        return utc_timestamp(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def capture_window(captures, due, decision):
    """Return independent visible legs and paired source/receipt integrity."""
    received, source, margins, sides = [], [], [], []
    for side, observed, captured, quality, trusted, uncertainty in captures:
        observed, captured = _timestamp(observed), _timestamp(captured)
        if (
            observed is None
            or captured is None
            or not due <= captured <= decision
            or not observed <= captured
            or decision - observed > MAX_SOURCE_AGE
            or quality != "HEALTHY"
            or trusted is not True
            or type(uncertainty) not in {int, float}
            or not 0 <= uncertainty <= 2000
        ):
            continue
        sides.append(side)
        received.append(captured)
        source.append(observed)
        margins.append(uncertainty)
    if len(sides) != 2:
        return sides, "POST_DELAY_CAUSAL_BOOK_EVIDENCE_MISSING"
    skew = max(abs(received[0] - received[1]), abs(source[0] - source[1]))
    if skew + timedelta(milliseconds=sum(margins)) > MAX_SKEW:
        return sides, "POST_DELAY_BOOK_SKEW_UNVERIFIED"
    return sides, None


def _bounded_projection(document, budget):
    evidence = document["evidence"]
    inputs = evidence.get("calculation_inputs") or {}
    bound = inputs.get("max_search_evaluations", MAX_SEARCH)
    bound = min(bound, MAX_SEARCH) if type(bound) is int and bound > 0 else MAX_SEARCH
    return replay_document(
        {
            **document,
            "evidence": {
                **evidence,
                "calculation_inputs": {
                    **inputs,
                    "max_search_evaluations": bound,
                },
            },
        },
        budget_cad=budget,
    )


def _conversion_limit(inputs, decision):
    for side in ("buy", "sell"):
        leg = inputs.get(side) or {}
        if leg.get("quote_currency") == "CAD":
            continue
        fx = leg.get("fx") or {}
        observed, received, expires = (
            _timestamp(fx.get(k)) for k in ("observed_at", "available_at", "fresh_until")
        )
        if not (observed and received and expires and observed <= received <= decision <= expires):
            return "POST_DELAY_CAD_CONVERSION_UNAVAILABLE"
    return None


def _project(row, *, origin, budget):
    at, result, evidence = row
    try:
        inputs = evidence.get("calculation_inputs") or {}
        decision = _timestamp(inputs.get("evaluated_at"))
        if decision is None or decision > at:
            return None, "PINNED_DECISION_TIME_UNAVAILABLE"
        if decision != origin and (reason := _conversion_limit(inputs, decision)):
            return None, reason
        report = _bounded_projection({**result, "evidence": evidence}, budget)
        item = report["decisions"][0]
        result = (item.get("result") or {}).get("best")
        if not result:
            return None, item.get("reason") or "EXECUTABLE_SIZE_INSUFFICIENT"
        if result.get("net_profit_cad") is None:
            return None, "CAD_NET_PROJECTION_UNAVAILABLE"
        return {
            "decision_at": item["decision_at"],
            "snapshot_ids": item["evidence_ids"],
            "best": result,
            "optimality": item["result"].get("optimality"),
        }, None
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None, "PINNED_FINANCIAL_EVIDENCE_UNAVAILABLE"


async def compare_delays(db, evaluation, now, budget):
    origin, route_id, eid = evaluation.evaluated_at, evaluation.route_id, evaluation.id
    original = (origin, evaluation.result, evaluation.evidence)
    # Keep this projection scalar. Read large calculation evidence only for the
    # first qualifying retained revision per delay, and release the DB before CPU.
    evidence = ArbitrageEvaluation.evidence
    inputs = evidence["calculation_inputs"]
    columns = [ArbitrageEvaluation.id, inputs["evaluated_at"].as_string()]
    for side in ("buy", "sell"):
        book = evidence[f"{side}_book"]
        clock = book["verification"]["clock"]
        columns.extend(
            (
                book["exchange_timestamp"].as_string(),
                book["received_at"].as_string(),
                book["state"].as_string(),
                clock["trusted"],
                clock["uncertainty_ms"],
            )
        )
    candidates = (
        await db.execute(
            select(*columns)
            .where(
                ArbitrageEvaluation.route_id == route_id,
                ArbitrageEvaluation.evaluated_at > origin,
                ArbitrageEvaluation.evaluated_at <= min(now, origin + timedelta(seconds=90)),
            )
            .order_by(ArbitrageEvaluation.evaluated_at, ArbitrageEvaluation.revision)
            .limit(MAX_REVISIONS + 1)
        )
    ).all()
    rows = []
    pins = {eid: original}
    for delay in DELAY_SECONDS:
        due = origin + timedelta(seconds=delay)
        scenario = {
            "delay_seconds": delay,
            "eligible_at": due.isoformat(),
            "reason": "POST_DELAY_CAUSAL_BOOK_EVIDENCE_MISSING",
            "observed_legs": [],
            "projection": None,
            "inventory_assessed": False,
            "executed": False,
        }
        choice = None
        if due > now:
            scenario["reason"] = "DELAYED_EVIDENCE_NOT_YET_AVAILABLE"
        elif delay == 0:
            choice = eid
            scenario["observed_legs"] = [
                side
                for side in ("buy", "sell")
                if _timestamp((original[2].get(f"{side}_book") or {}).get("received_at"))
                is not None
            ]
        else:
            sides_seen = set()
            for row in candidates[:MAX_REVISIONS]:
                candidate_id, timestamp, *fields = row
                decision = _timestamp(timestamp)
                if decision is None or not due <= decision <= min(now, due + RETAINED_WINDOW):
                    continue
                captures = (("buy", *fields[:5]), ("sell", *fields[5:]))
                sides, reason = capture_window(captures, due, decision)
                sides_seen.update(sides)
                if reason is not None:
                    if len(sides) == 2:
                        scenario["reason"] = reason
                    continue
                choice = candidate_id
                break  # Sparse retention provides only the earliest eligible retained revision.
            scenario["observed_legs"] = sorted(sides_seen)
            if choice is None and len(candidates) > MAX_REVISIONS:
                scenario["reason"] = "RETAINED_REVISION_WINDOW_LIMITED"
        if choice is not None and choice not in pins:
            retained = await db.get(ArbitrageEvaluation, choice)
            if retained is not None:
                pins[choice] = retained.evaluated_at, retained.result, retained.evidence
        rows.append((scenario, choice))
    await db.rollback()
    for scenario, choice in rows:
        if choice in pins:
            scenario["projection"], scenario["reason"] = await asyncio.to_thread(
                _project,
                pins[choice],
                origin=origin,
                budget=budget,
            )
            scenario["evaluation_id"] = str(choice)
    return {
        "schema_version": "paper-research-delay-matrix-v1",
        "evaluation_id": str(eid),
        "detected_at": origin.isoformat(),
        "budget_cad": str(budget),
        "paper_only": True,
        "actionable": False,
        "items": [scenario for scenario, _ in rows],
        "limitations": [
            "Later rows require the first qualifying retained paired revision with its own inputs",
            "Independent observed legs can lack synchronized evidence for a paired projection",
            "Exact retained paper fills and ledger movements remain separate from projections",
        ],
    }
