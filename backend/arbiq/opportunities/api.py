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
from arbiq.opportunities.classification import CONFIDENCE, classify_route
from arbiq.opportunities.classification import financial_sort as financial_sort
from arbiq.opportunities.diagnostics import size_diagnostics

router = APIRouter(prefix="/opportunities", tags=["arbitrage research"])
CANDIDATE_WINDOW = 500
CUSTOM_BUDGET_WINDOW = 50
COLLECTOR_RETRY_SECONDS = 2
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


class CollectorOutage:
    def __init__(self):
        self.retry_at: float | None = None
        self.last_contact: float | None = None
        self.probe_active = False


async def collector_engine_json(request: Request, path: str, **kwargs) -> dict | None:
    """Share only transport failure timing. Every live result comes from its own IPC reply."""
    app_state = request.app.state
    client = app_state.collector_http
    request_state = getattr(request, "state", None)
    if client is None or getattr(request_state, "collector_unavailable", False):
        return None
    outage = getattr(app_state, "collector_engine_outage", None)
    if outage is None:
        outage = app_state.collector_engine_outage = CollectorOutage()
    started = elapsed_seconds()
    retry_at = outage.retry_at
    recovering = retry_at is not None
    if recovering:
        if outage.probe_active or started < retry_at:
            if request_state is not None:
                request_state.collector_unavailable = True
            return None
        outage.probe_active = True

    value = None
    try:
        async with client.get(path, **kwargs) as response:
            unavailable = response.status in {502, 503, 504}
            if response.status == 200:
                try:
                    value = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    # The endpoint returned a reply. Its peer diagnostics may
                    # still be usable even when this payload is malformed.
                    pass
    except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError):
        unavailable = True
    except (aiohttp.ClientError, ValueError):
        return None
    finally:
        if recovering:
            outage.probe_active = False

    finished = elapsed_seconds()
    if unavailable:
        if request_state is not None:
            request_state.collector_unavailable = True
        if outage.last_contact is None or started >= outage.last_contact:
            outage.retry_at = finished + COLLECTOR_RETRY_SECONDS
        return None
    outage.last_contact = finished
    outage.retry_at = None
    return value if isinstance(value, dict) else None


async def collector_current(
    request: Request, *, full=True, route_id=None, route_ids=None
) -> tuple[dict, dict | None]:
    params = {"summary": "0" if full else "1"}
    if route_id:
        params["route_id"] = str(route_id)
    elif route_ids and len(route_ids) <= CUSTOM_BUDGET_WINDOW:
        params["route_ids"] = ",".join(map(str, route_ids))
    value = await collector_engine_json(request, "/engine/current", params=params)
    if value is not None and isinstance(value.get("items"), list):
        items = {
            item["id"]: item
            for item in value["items"][:5000]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        diagnostics = value.get("diagnostics")
        return items, live_diagnostics(diagnostics) if isinstance(diagnostics, dict) else None
    return {}, None


async def collector_diagnostics(request: Request, db) -> dict:
    """Live IPC proves process liveness; persisted heartbeats cannot prove UTC trust."""
    value = await collector_engine_json(request, "/engine/diagnostics")
    if value is not None:
        return live_diagnostics(value)
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
    maximum_book_skew: int | None = None,
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
    payload["data_confidence"] = max(quality, key=lambda x: CONFIDENCE.get(x, 4))
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
    if payload["status"] == "EXPIRED" or source_expired or (age is not None and age < 0):
        freshness_state = "STALE"
    elif not active:
        freshness_state = "RETAINED"
    elif (
        payload["status"] not in TERMINAL
        and diagnostics.get("healthy")
        and diagnostics.get("clock_healthy")
        and age is not None
        and supporting_received_at is not None
        and "FRESHNESS_CLASS_NOT_VERIFIABLE" not in reasons
        and "QUOTE_INGESTION_NOT_CURRENT" not in reasons
    ):
        freshness_state = "CURRENT"
    else:
        freshness_state = "UNVERIFIED"
    payload["classifications"] = classify_route(
        payload,
        calculation_evidence if isinstance(calculation_evidence, dict) else {},
        freshness=freshness_state,
        maximum_book_skew=maximum_book_skew,
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


@router.get("/diagnostics")
async def diagnostics(request: Request, db: DB, user: AuthenticatedUser):
    return {
        **await collector_diagnostics(request, db),
        "api_candidate_window": CANDIDATE_WINDOW,
        "actionable": False,
    }


@router.get("")
async def opportunities(
    request: Request,
    response: Response,
    db: DB,
    user: AuthenticatedUser,
    budget_cad: Budget = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=CANDIDATE_WINDOW),
    sort: Literal["net_profit", "net_return", "liquidity"] = "net_profit",
    exchange: str = Query(default="", max_length=80, pattern="^[a-z0-9_-]*$"),
    asset: str = Query(default="", max_length=32),
    status: Status | None = None,
    history: bool = False,
    search: str = Query(default="", max_length=80),
    min_profit_cad: ProfitFilter = None,
    min_return_pct: ReturnFilter = None,
    min_quantity: NonNegativeFilter = None,
    min_liquidity_cad: NonNegativeFilter = None,
    max_acquisition_cad: CapitalFilter = None,
    confidence: Literal["HEALTHY", "DEGRADED", "SUSPICIOUS", "STALE", "UNAVAILABLE"] | None = None,
):
    config = request.app.state.settings.arbitrage
    budget = budget_cad if budget_cad is not None else config.default_budget_cad
    rank_by = {
        "net_profit": "net_profit_cad",
        "net_return": "net_return_pct",
        "liquidity": "maximum_liquidity_cad",
    }[sort]
    custom_budget = budget != config.default_budget_cad
    window = CUSTOM_BUDGET_WINDOW if custom_budget else CANDIDATE_WINDOW
    gate = _projection_gate if custom_budget else _view_gate
    if gate.locked():
        raise HTTPException(
            429,
            "A candidate sizing request is running; retry shortly",
            headers={"Retry-After": "2"},
        )
    async with gate:
        started_at = elapsed_seconds()
        criteria = []
        if exchange:
            criteria.append(
                or_(
                    ArbitrageRoute.payload["buy_exchange"].as_string() == exchange,
                    ArbitrageRoute.payload["sell_exchange"].as_string() == exchange,
                )
            )
        if asset:
            criteria.append(ArbitrageRoute.payload["base_asset"].as_string() == asset.upper())
        matching = await db.scalar(
            select(func.count()).select_from(ArbitrageRoute).where(*criteria)
        )
        query = list_route_query(budget, include_payload=False).where(*criteria)
        rows = (
            await db.execute(
                query.order_by(ArbitrageRoute.updated_at.desc(), ArbitrageRoute.id).limit(window)
            )
        ).all()
        # Return the authentication/list lease before IPC. These scalar rows are
        # independent of the session and preserve the original candidate window.
        await db.commit()
        selected_at = elapsed_seconds()
        current, health = {}, {}
        if rows:
            current, health = await collector_current(
                request,
                full=custom_budget,
                route_ids=[row.id for row in rows] if custom_budget else None,
            )
            health = health or await collector_diagnostics(request, db)
        checked_at = elapsed_seconds()

        candidates = []
        retained_ids = []
        for row in rows:
            item = current.get(str(row.id))
            if live_projection_times(item) is None:
                item = None
            effective_status = item.get("status", row.status) if item is not None else row.status
            # The ordinary projection never revives a terminal status. A usable
            # live replacement can override the retained lifecycle before this test.
            if not history and effective_status in TERMINAL:
                continue
            candidates.append((row, item))
            if item is None:
                retained_ids.append(row.id)

        retained = {}
        if retained_ids:
            retained_query = list_route_query(budget, full=custom_budget).where(
                ArbitrageRoute.id.in_(retained_ids)
            )
            # Read the status and times together with each retained payload so a
            # concurrent collector revision cannot mix their evidence.
            retained = {row.id: row for row in (await db.execute(retained_query)).all()}
        await db.commit()
        loaded_at = elapsed_seconds()

        def projections(candidates, retained):
            result = []
            for row, item in candidates:
                if item is None:
                    row = retained.get(row.id)
                    if row is None:
                        continue
                projected = project(
                    row,
                    budget,
                    health,
                    config.maximum_book_age_seconds,
                    current=item,
                    max_resize_evaluations=config.max_optimizer_evaluations,
                    maximum_book_skew=config.maximum_book_skew_seconds,
                )
                effective = projected["status"]
                if (not history and effective in TERMINAL) or (status and effective != status):
                    continue
                if not meets_filters(
                    projected,
                    search=search,
                    min_profit_cad=min_profit_cad,
                    min_return_pct=min_return_pct,
                    min_quantity=min_quantity,
                    min_liquidity_cad=min_liquidity_cad,
                    max_acquisition_cad=max_acquisition_cad,
                    confidence=confidence,
                ):
                    continue
                result.append({k: v for k, v in projected.items() if k not in LIST_DETAIL_FIELDS})
            result.sort(key=lambda row: financial_sort(row, rank_by))
            return result[offset : offset + limit], len(result)

        items, total = (
            await asyncio.to_thread(projections, candidates, retained) if candidates else ([], 0)
        )
        projected_at = elapsed_seconds()
    response.headers["Server-Timing"] = ", ".join(
        (
            f'catalog;dur={(selected_at - started_at) * 1000:.2f};desc="{len(rows)} rows"',
            f"collector;dur={(checked_at - selected_at) * 1000:.2f}",
            f'retained;dur={(loaded_at - checked_at) * 1000:.2f};desc="{len(retained)} rows"',
            f"projection;dur={(projected_at - loaded_at) * 1000:.2f}",
        )
    )
    # Release retained/input trees before serializing the small display rows.
    del rows, current, candidates, retained
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "matching_retained_routes": matching,
        "candidate_window": window,
        "window_limited": matching > window,
        "sizing_search_limit": config.max_optimizer_evaluations,
        "sizing_scope": "Recent bounded candidate window; projected optimum may be uncertified",
        "ranking_scope": "Financial metric, quality, freshness, capacity and verification",
        "selected_budget_cad": str(budget),
        "source": "live_and_retained_calculation_evidence",
    }


@router.get("/{route_id}")
async def detail(
    route_id: UUID,
    request: Request,
    db: DB,
    user: AuthenticatedUser,
    budget_cad: Budget = None,
):
    route = await db.get(ArbitrageRoute, route_id)
    if route is None:
        raise HTTPException(404, "Opportunity not found")
    latest = await db.scalar(
        select(ArbitrageEvaluation)
        .where(ArbitrageEvaluation.route_id == route_id)
        .order_by(ArbitrageEvaluation.revision.desc())
        .limit(1)
    )
    lifecycle = (
        await db.scalars(
            select(ArbitrageLifecycleEvent)
            .where(ArbitrageLifecycleEvent.route_id == route_id)
            .order_by(
                ArbitrageLifecycleEvent.event_at.desc(),
                func.coalesce(ArbitrageLifecycleEvent.details["revision"].as_integer(), 0).desc(),
                ArbitrageLifecycleEvent.id,
            )
            .limit(100)
        )
    ).all()
    config = request.app.state.settings.arbitrage
    current, health = await collector_current(request, route_id=route_id)
    active = current.get(str(route_id))
    selected = budget_cad if budget_cad is not None else config.default_budget_cad
    try:
        requires_sizing = (
            Decimal(str((active or route.payload).get("selected_budget_cad"))) != selected
        )
    except (ValueError, TypeError, InvalidOperation):
        requires_sizing = True
    gate = _projection_gate if requires_sizing else _view_gate
    if gate.locked():
        raise HTTPException(
            429, "Candidate sizing busy; retry shortly", headers={"Retry-After": "2"}
        )
    async with gate:
        projected = await asyncio.to_thread(
            project,
            route,
            budget_cad if budget_cad is not None else config.default_budget_cad,
            health or await collector_diagnostics(request, db),
            config.maximum_book_age_seconds,
            evidence=latest.evidence if latest else None,
            current=active,
            maximum_book_skew=config.maximum_book_skew_seconds,
        )
    return {
        **projected,
        "evaluation_id": str(latest.id) if latest and not active else None,
        "retained_evaluation_id": str(latest.id) if latest else None,
        "revision": route.revision,
        "evaluated_at": latest.evaluated_at if latest else None,
        "lifecycle": [
            {
                "id": str(event.id),
                "status": event.status,
                "event_at": event.event_at,
                "reason": event.reason,
                "details": event.details,
            }
            for event in reversed(lifecycle)
        ],
        "lifecycle_limit": 100,
        "official_link_limitation": "Only individually verified official market pages are linked",
    }


@router.post("/{route_id}/recheck", status_code=202)
async def recheck(route_id: UUID, request: Request, db: DB, user: AuthenticatedUser):
    if await db.get(ArbitrageRoute, route_id) is None:
        raise HTTPException(404, "Opportunity not found")
    client = request.app.state.collector_http
    if client is None:
        raise HTTPException(503, "Collector unavailable")
    try:
        async with client.post(f"/engine/routes/{route_id}/recheck") as response:
            if response.status != 202:
                code = response.status if response.status in {404, 409, 429, 503} else 502
                raise HTTPException(code, "Recheck unavailable or rate limited")
            db.add(
                SystemEvent(
                    actor_user_id=user.id,
                    event_type="opportunity.recheck_requested",
                    severity="info",
                    message="Owner requested fresh public market evidence",
                    details={"route_id": str(route_id), "mode": "paper_research"},
                )
            )
            await db.commit()
            return {"status": "QUEUED", "actionable": False}
    except (aiohttp.ClientError, TimeoutError):
        raise HTTPException(503, "Collector unavailable") from None


@router.get("/{route_id}/size-diagnostics")
async def diagnostics_sizes(
    route_id: UUID,
    request: Request,
    db: DB,
    user: AuthenticatedUser,
    budget_cad: Budget = None,
):
    if _projection_gate.locked():
        raise HTTPException(
            429, "Candidate sizing busy; retry shortly", headers={"Retry-After": "2"}
        )
    try:
        async with _projection_gate:
            evaluation = (
                await db.execute(
                    select(ArbitrageEvaluation.id, ArbitrageEvaluation.evidence)
                    .where(ArbitrageEvaluation.route_id == route_id)
                    .order_by(ArbitrageEvaluation.revision.desc())
                    .limit(1)
                )
            ).one_or_none()
            if evaluation is None:
                if await db.get(ArbitrageRoute, route_id) is None:
                    raise HTTPException(404, "Opportunity not found")
                raise HTTPException(409, "Retained sizing evidence unavailable")
            evaluation_id, evidence = evaluation
            await db.commit()
            config = request.app.state.settings.arbitrage
            value = await asyncio.to_thread(
                size_diagnostics,
                evidence,
                budget_cad if budget_cad is not None else config.default_budget_cad,
                max_search_evaluations=config.max_optimizer_evaluations,
            )
    except (ValueError, KeyError, TypeError, ArithmeticError):
        raise HTTPException(409, "Retained sizing evidence cannot be reconstructed") from None
    return {**value, "route_id": str(route_id), "evaluation_id": str(evaluation_id)}
