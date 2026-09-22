"""Bounded, historical size diagnostics from pinned depth. No writes or forecasts."""

from dataclasses import asdict
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from arbiq.market_data.quality import safe_json
from arbiq.opportunities.analysis import load_costs, load_input
from arbiq.opportunities.math import (
    InsufficientDepth,
    convert,
    evaluate_route,
    maximum_quantity,
    optimize_route,
    precision,
    quantity_for_notional,
)

COMPARISON_CAPITALS = tuple(Decimal(x) for x in (50, 100, 250, 500, 1000))


def projection(value):
    if value is None:
        return None
    return safe_json(asdict(value))


