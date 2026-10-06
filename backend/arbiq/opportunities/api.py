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


