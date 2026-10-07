"""Authenticated research views over immutable calculation evidence.

Repricing a budget uses the original decision inputs. It never turns a retained
calculation into a new market observation, and does not imply an execution.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from uuid import UUID

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import ARRAY, Text, and_, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.connectors.market_links import official_market_link
from arbiq.core.clock import elapsed_seconds
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    CollectorRun,
    SystemEvent,
)
from arbiq.opportunities.analysis import resize
from arbiq.opportunities.diagnostics import size_diagnostics

router = APIRouter(prefix="/opportunities", tags=["arbitrage research"])
CANDIDATE_WINDOW = 500
CUSTOM_BUDGET_WINDOW = 50
# Lists never return these fields. Preserve the full immutable inputs in detail,
# custom sizing, diagnostics and exports, while avoiding large decoded Python trees
# for same-budget list freshness/quality/filter projection.
LIST_DETAIL_FIELDS = frozenset(
    {
        "costs",
        "evidence",
        "budget_results",
        "maximum_executable",
        "transfer_scenario",
        "optimizer",
    }
)
LIST_UNUSED_FIELDS = ("budget_results", "transfer_scenario", "optimizer")
LIST_UNUSED_EVIDENCE_PATHS = (
    ("evidence", "fx_books"),
    *(
        ("evidence", "calculation_inputs", side, key)
        for side in ("buy", "sell")
        for key in ("asks", "bids")
    ),
    *(
        ("evidence", "calculation_inputs", side, "fx", direction)
        for side in ("buy", "sell")
        for direction in ("to_cad", "from_cad")
    ),
)
LIST_KNOWN_SHAPE = (
    "$ ? ("
    + " && ".join(
        [
            f'@.{path}.type() == "object"'
            for path in (
                "evidence",
                "evidence.calculation_inputs",
                "evidence.calculation_inputs.buy",
                "evidence.calculation_inputs.sell",
            )
        ]
        + [
            f"(!exists(@.evidence.calculation_inputs.{side}.fx) || "
            f'@.evidence.calculation_inputs.{side}.fx.type() == "object" || '
            f'@.evidence.calculation_inputs.{side}.fx.type() == "null")'
            for side in ("buy", "sell")
        ]
    )
    + ")"
)
_projection_gate = asyncio.Semaphore(1)
_view_gate = asyncio.Semaphore(3)
Status = Literal[
    "DETECTED", "RECHECKING", "VERIFIED_MARKET", "RESEARCH_ONLY", "INVALIDATED", "EXPIRED"
]
TERMINAL = {"INVALIDATED", "EXPIRED"}
Budget = Annotated[Decimal | None, Query(gt=0, le=1000000, decimal_places=2)]
ProfitFilter = Annotated[Decimal | None, Query(ge=-1000000000, le=1000000000)]
ReturnFilter = Annotated[Decimal | None, Query(ge=-1000000, le=1000000)]
NonNegativeFilter = Annotated[Decimal | None, Query(ge=0, le=1000000000)]
CapitalFilter = Annotated[Decimal | None, Query(gt=0, le=1000000000)]


def list_payload_expression(budget: Decimal, *, full: bool = False, payload_column=None):
    """Prune unused depth in PostgreSQL before JSON decoding, never in storage.

    A retained budget mismatch always selects the original full JSON, so the
    existing deterministic resize path still has every pinned input. Literal
    string equality is deliberately conservative: equivalent unusual formatting
    falls back to full evidence rather than risking an incomplete recalculation.
    """
    original = ArbitrageRoute.payload if payload_column is None else payload_column
    if full:
        return original
    compact = original.op("-")(cast(list(LIST_UNUSED_FIELDS), ARRAY(Text)))
    for path in LIST_UNUSED_EVIDENCE_PATHS:
        compact = compact.op("#-")(cast(list(path), ARRAY(Text)))
    # PostgreSQL path deletion interprets a string key as an array index when a
    # malformed ancestor is an array. Preserve the original fallback for unknown
    # shapes instead of turning an unavailable projection into a SQL exception.
    # One JSONPath shape probe avoids materializing the large evidence subtree
    # repeatedly merely to inspect each nested container's type.
    known_shape = func.jsonb_path_exists(original, cast(LIST_KNOWN_SHAPE, JSONPATH))
    return case(
        (and_(original["selected_budget_cad"].as_string() == str(budget), known_shape), compact),
        else_=original,
    )


def list_route_query(budget: Decimal, *, full: bool = False, include_payload: bool = True):
    """Lightweight rows avoid hydrating unused ORM columns and identity-map state."""
    columns = (
        ArbitrageRoute.id,
        ArbitrageRoute.status,
        ArbitrageRoute.detected_at,
        ArbitrageRoute.updated_at,
        ArbitrageRoute.expires_at,
    )
    if include_payload:
        columns += (list_payload_expression(budget, full=full).label("payload"),)
    return select(*columns)


def utc_timestamp(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None:
            return None
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def live_projection_times(value) -> tuple[datetime, datetime] | None:
    if isinstance(value, dict):
        updated_at = utc_timestamp(value.get("updated_at"))
        expires_at = utc_timestamp(value.get("expires_at"))
        if updated_at is not None and expires_at is not None:
            return updated_at, expires_at
    return None


def live_diagnostics(value: dict) -> dict:
    clock = value.get("clock", {})
    if not isinstance(clock, dict):
        clock = {"trusted": False, "host_healthy": False, "reason": "CLOCK_DIAGNOSTICS_INVALID"}
    trustworthy = (
        isinstance(clock, dict)
        and clock.get("trusted") is True
        and utc_timestamp(clock.get("utc_now")) is not None
    )
    return {
        **value,
        "healthy": value.get("healthy") is True,
        "clock": clock,
        "source": "live_collector_engine",
        "clock_healthy": trustworthy and clock.get("host_healthy") is True,
    }


async def collector_current(
    request: Request, *, full=True, route_id=None, route_ids=None
) -> tuple[dict, dict | None]:
    client = request.app.state.collector_http
    if client is not None:
        try:
            params = {"summary": "0" if full else "1"}
            if route_id:
                params["route_id"] = str(route_id)
            elif route_ids and len(route_ids) <= CUSTOM_BUDGET_WINDOW:
                params["route_ids"] = ",".join(map(str, route_ids))
            async with client.get("/engine/current", params=params) as response:
                if response.status == 200:
                    value = await response.json()
                    if isinstance(value, dict) and isinstance(value.get("items"), list):
                        items = {
                            item["id"]: item
                            for item in value["items"][:5000]
                            if isinstance(item, dict) and isinstance(item.get("id"), str)
                        }
                        diagnostics = value.get("diagnostics")
                        return items, live_diagnostics(diagnostics) if isinstance(
                            diagnostics, dict
                        ) else None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            pass
    return {}, None


async def collector_diagnostics(request: Request, db) -> dict:
    """Live IPC proves process liveness; persisted heartbeats cannot prove UTC trust."""
    client = request.app.state.collector_http
    if client is not None and not getattr(
        getattr(request, "state", None), "collector_unavailable", False
    ):
        try:
            async with client.get("/engine/diagnostics") as response:
                if response.status == 200:
                    value = await response.json()
                    if isinstance(value, dict):
                        return live_diagnostics(value)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            pass
    run = await db.scalar(
        select(CollectorRun)
        .order_by((CollectorRun.status == "running").desc(), CollectorRun.created_at.desc())
        .limit(1)
    )
    retained = (run.metrics or {}).get("engine", {}) if run else {}
    return {
        **(retained if isinstance(retained, dict) else {}),
        "healthy": False,
        "clock_healthy": False,
        "clock": {"trusted": False, "host_healthy": False, "reason": "LIVE_CLOCK_UNAVAILABLE"},
        "source": "persisted_engine_diagnostics" if run else "unavailable",
        "heartbeat_at": run.heartbeat_at.isoformat() if run else None,
        "reasons": ["LIVE_ENGINE_UNAVAILABLE", "PERSISTED_EVIDENCE_IS_NOT_A_FRESH_QUOTE"],
    }


def project(
    route,
    budget: Decimal,
    diagnostics: dict,
    maximum_age: int,
    evidence=None,
    current=None,
    max_resize_evaluations=4096,
) -> dict:
    """Apply current display eligibility without modifying historical audit records."""
    live_times = live_projection_times(current)
    active = current if live_times else None
    updated_at, expires_at = live_times or (route.updated_at, route.expires_at)
    status = active.get("status", route.status) if active else route.status
    payload = {
        **(active if active else route.payload),
        "id": str(route.id),
        "status": status,
        "detected_at": route.detected_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "actionable": False,
        "eligibility": "UNVERIFIED",
    }
    if evidence is not None and not active:
        payload["evidence"] = evidence
    elif evidence is not None and active:
        retained = {
            key: value
            for key, value in evidence.items()
            if key.endswith(("_snapshot_id", "_fee_version_id"))
        }
        for side in ("buy", "sell"):
            book = evidence.get(f"{side}_book", {})
            if isinstance(book, dict):
                retained[f"{side}_received_at"] = book.get("received_at")
        payload["evidence"] = {
            **payload.get("evidence", {}),
            "retained_snapshot_references": {
                **retained,
                "attribution": "last durable evaluation; may precede current market inputs",
            },
        }
    try:
        retained_budget = Decimal(str(payload.get("selected_budget_cad", "NaN")))
        if retained_budget != budget:
            payload = resize(payload, budget, max_search_evaluations=max_resize_evaluations)
    except (ValueError, TypeError, ArithmeticError, KeyError):
        payload = {
            **payload,
            "quantity": None,
            "buy_price": None,
            "sell_price": None,
            "net_profit_cad": None,
            "net_return_pct": None,
            "acquisition_cost_cad": None,
            "costs": {},
            "reasons": [*payload.get("reasons", []), "CALCULATION_EVIDENCE_UNAVAILABLE"],
        }
    reasons = list(payload.get("reasons", []))
    clock = diagnostics.get("clock", {})
    if not isinstance(clock, dict):
        clock = {}
    now = utc_timestamp(clock.get("utc_now")) if clock.get("trusted") is True else None
    supporting_received_at = utc_timestamp(payload.get("supporting_received_at"))
    if supporting_received_at is None:
        reasons.append("SUPPORTING_BOOK_TIMESTAMPS_UNAVAILABLE")
    age_reference = supporting_received_at or updated_at
    age = (now - age_reference).total_seconds() if now else None
    payload["age_seconds"] = (
        (now - route.detected_at).total_seconds() if now and now >= route.detected_at else None
    )
    payload["evidence_age_seconds"] = age if age is not None and age >= 0 else None
    calculation_evidence = payload.get("evidence", {})
    inputs = (
        calculation_evidence.get("calculation_inputs", {})
        if isinstance(calculation_evidence, dict)
        else {}
    )
    inputs = inputs if isinstance(inputs, dict) else {}
    input_time = utc_timestamp(inputs.get("evaluated_at")) or updated_at
    payload["costs_evaluated_at"] = input_time.isoformat()
    payload["costs_attribution"] = "historical projection from retained inputs at evaluation time"
    cad_suppression = []
    for side in ("buy", "sell"):
        leg = inputs.get(side, {})
        if not isinstance(leg, dict) or leg.get("quote_currency") in {None, "CAD"}:
            continue
        fx = leg.get("fx")
        fresh_until = utc_timestamp(fx.get("fresh_until")) if isinstance(fx, dict) else None
        if fresh_until is None:
            cad_suppression.append("CAD_CONVERSION_EVIDENCE_UNAVAILABLE")
        elif now is None:
            cad_suppression.append("CAD_CONVERSION_FRESHNESS_UNVERIFIED")
        elif now >= fresh_until:
            cad_suppression.append("CAD_CONVERSION_EXPIRED")
    supporting_source_at = utc_timestamp(payload.get("supporting_source_at"))
    if supporting_source_at is None and isinstance(calculation_evidence, dict) and not active:
        books = [calculation_evidence.get(f"{side}_book", {}) for side in ("buy", "sell")]
        source_times = [
            utc_timestamp(book.get("exchange_timestamp"))
            for book in books
            if isinstance(book, dict)
        ]
        if len(source_times) == 2 and all(source_times):
            supporting_source_at = min(source_times)
    source_age = (
        (now - supporting_source_at).total_seconds() if now and supporting_source_at else None
    )
    source_expired = source_age is not None and (source_age > maximum_age or source_age < 0)
    payload["source_evidence_age_seconds"] = source_age
    if source_expired:
        cad_suppression.append("SUPPORTING_SOURCE_TIMESTAMP_NOT_CURRENT")
    if status not in TERMINAL:
        if now and (now >= expires_at or (age is not None and age > maximum_age)):
            payload["status"] = "EXPIRED"
            reasons.append("RETAINED_CALCULATION_EXPIRED")
        elif now and age is not None and age < 0:
            payload["status"] = "RESEARCH_ONLY"
            reasons.append("EVALUATION_TIMESTAMP_IN_FUTURE")
        elif not diagnostics.get("healthy") or now is None:
            payload["status"] = "RESEARCH_ONLY"
            reasons.append("LIVE_FRESHNESS_UNVERIFIED")
        elif not diagnostics.get("clock_healthy"):
            payload["status"] = "RESEARCH_ONLY"
            reasons.append("HOST_CLOCK_UNHEALTHY")
        elif supporting_received_at is None:
            payload["status"] = "RESEARCH_ONLY"
        if source_expired:
            payload["status"] = "EXPIRED"
        elif cad_suppression and payload["status"] not in TERMINAL:
            payload["status"] = "RESEARCH_ONLY"
    if cad_suppression:
        payload["net_profit_cad"] = None
        payload["net_return_pct"] = None
        payload["acquisition_cost_cad"] = None
        reasons.extend(cad_suppression)
    payload["cad_valuation_state"] = "SUPPRESSED" if cad_suppression else "RETAINED_PROJECTION"
    if isinstance(payload.get("costs"), dict) and payload["costs"]:
        payload["costs"] = {
            **payload["costs"],
            "evaluated_at_utc": input_time.isoformat(),
            "attribution": "Historical projection at evaluation time; no execution",
            "current_cad_valuation": ", ".join(dict.fromkeys(cad_suppression))
            if cad_suppression
            else "Retained projection; assess all confidence reasons",
        }
    payload["reasons"] = list(dict.fromkeys(reasons))
    payload["selected_budget_cad"] = str(budget)
    payload["calculation_attribution"] = "retained evaluation; projected values, no execution"
    payload["storage_origin"] = "live_engine_projection" if active else "retained_evaluation"
    payload["official_market_links"] = {
        side: official_market_link(payload.get(f"{side}_exchange"), payload.get(f"{side}_symbol"))
        for side in ("buy", "sell")
    }
    quality = [
        calculation_evidence.get(f"{side}_quality", "UNAVAILABLE") for side in ("buy", "sell")
    ]
    severity = {"HEALTHY": 0, "DEGRADED": 1, "SUSPICIOUS": 2, "STALE": 3, "UNAVAILABLE": 4}
    payload["data_confidence"] = max(quality, key=lambda x: severity.get(x, 4))
    costs = payload.get("costs") or {}
    maximum = payload.get("maximum_executable") or {}
    payload["required_inventory"] = costs.get("capital_requirements", {})
    payload["estimated_costs"] = {
        k: costs.get(k)
        for k in ("fees_cad", "fx_cost_cad", "slippage_reserve_cad", "rebalancing_cost_cad")
    }
    payload["maximum_liquidity_cad"] = (
        maximum.get("acquisition_cost_cad") if not cad_suppression else None
    )
    payload["liquidity_definition"] = (
        "Modeled depth-constrained acquisition cost; not available funds"
    )
    return payload


def meets_filters(
    row,
    *,
    search="",
    min_profit_cad=None,
    min_return_pct=None,
    min_quantity=None,
    min_liquidity_cad=None,
    max_acquisition_cad=None,
    confidence=None,
):
    if (
        search
        and search.casefold()
        not in " ".join(
            str(row.get(k, ""))
            for k in ("base_asset", "buy_exchange", "sell_exchange", "buy_symbol", "sell_symbol")
        ).casefold()
    ):
        return False
    if confidence and row.get("data_confidence") != confidence:
        return False
    for field, target, minimum in (
        ("net_profit_cad", min_profit_cad, True),
        ("net_return_pct", min_return_pct, True),
        ("quantity", min_quantity, True),
        ("maximum_liquidity_cad", min_liquidity_cad, True),
        ("acquisition_cost_cad", max_acquisition_cad, False),
    ):
        if target is None:
            continue
        try:
            amount = Decimal(row[field])
            if not amount.is_finite() or (amount < target if minimum else amount > target):
                return False
        except (KeyError, TypeError, InvalidOperation):
            return False
    return True


def financial_sort(value: dict, field: str) -> tuple:
    raw = value.get(field)
    try:
        number = Decimal(raw) if isinstance(raw, str) else None
        if number is not None and not number.is_finite():
            number = None
    except InvalidOperation:
        number = None
    return (number is None, number.copy_negate() if number is not None else Decimal(0), value["id"])


@router.get("/budgets")
async def budgets(request: Request, user: AuthenticatedUser):
    config = request.app.state.settings.arbitrage
    return {
        "budgets_cad": [str(value) for value in config.budgets_cad],
        "default_budget_cad": str(config.default_budget_cad),
        "minimum_cad": "0.01",
        "maximum_cad": "1000000",
        "mode": "paper_research",
    }


