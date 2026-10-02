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


