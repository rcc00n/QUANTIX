"""Causal exact-depth sizing for an on-demand paper allocation, never a fill."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from arbiq.opportunities.analysis import load_input
from arbiq.opportunities.contracts import Costs, Evaluation, Fee, MarketInput
from arbiq.opportunities.math import (
    conversion_capacity,
    convert,
    evaluate_route,
    fill_depth,
    maximum_quantity,
    quantity_for_notional,
    required_input,
)
from arbiq.paper_trading.execution import planned_reserves
from arbiq.portfolio.contracts import SCALE, decimal_context
from arbiq.settings.strategy import PaperStrategy

ONE = Decimal(1)
ZERO = Decimal(0)
TRADE_SIZES = tuple(Decimal(value) for value in (50, 100, 250, 500, 1000, 2500, 5000))


def time_value(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("EVIDENCE_TIMESTAMP_MISSING")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() != timedelta(0):
        raise ValueError("EVIDENCE_TIMESTAMP_NOT_UTC")
    return result


@dataclass(frozen=True, slots=True)
class PinnedPair:
    buy: MarketInput
    sell: MarketInput
    at: datetime
    costs: Costs
    maximum_age_seconds: int
    reliability: Decimal
    provenance: dict


@dataclass(frozen=True, slots=True)
class SizedPair:
    evaluation: Evaluation
    acquisition_cad: Decimal
    reserved_acquisition_cad: Decimal
    buy_quote: Decimal
    sell_base: Decimal
    buy_mark_rate: Decimal
    sell_mark_rate: Decimal


def projected_costs(evaluation: Evaluation) -> dict[str, str | None]:
    return {
        key: format(value, "f") if value is not None else None
        for key, value in {
            "fees_cad": evaluation.fees_cad,
            "fx_drag_cad": evaluation.fx_cost_cad,
            "slippage_reserve_cad": evaluation.slippage_reserve_cad,
            "rebalancing_cad": evaluation.rebalancing_cost_cad,
        }.items()
    }


def checked_book(book: dict, at: datetime, age: timedelta, allow_receipt_only: bool) -> bool:
    receipt = time_value(book["received_at"])
    source = time_value(book["exchange_timestamp"]) if book.get("exchange_timestamp") else None
    verification = book.get("verification", {})
    if book.get("state") not in {"HEALTHY", "DEGRADED"}:
        raise ValueError("BOOK_QUALITY_BLOCKS_ALLOCATION")
    if (
        not timedelta(0) <= at - receipt <= age
        or (source is not None and (source > receipt or not timedelta(0) <= at - source <= age))
        or verification.get("clock", {}).get("trusted") is False
        or verification.get("last_error_code")
    ):
        raise ValueError("STALE_UNTRUSTED_OR_FUTURE_BOOK")
    if source is None and not allow_receipt_only:
        raise ValueError("SOURCE_TIMESTAMP_MISSING")
    return source is not None and book["state"] == "HEALTHY"


