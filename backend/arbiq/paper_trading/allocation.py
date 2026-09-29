"""Bounded allocation of independent, pre-priced two-leg paper inventory.

Route sizing and executable-depth valuation happen before this pure calculation.
The planner never interpolates marks or assumes parity between quote assets.
"""

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from arbiq.finance.contracts import exact_decimal, utc_timestamp
from arbiq.portfolio.contracts import SCALE, decimal_context, stored

AllocationPolicy = Literal["LIQUIDITY_WEIGHTED", "ROUTE_OPTIMIZED", "DIVERSIFIED_HYBRID"]
VERSION = "quantix-allocation-v2"
TRADE_SIZES = tuple(Decimal(value) for value in (50, 100, 250, 500, 1000, 2500, 5000))
CAD = "fiat:CAD"
ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
MAX_ROUTES = 40
MAX_ROUTE_OPTIONS = 360
MAX_VENUES = 30
MAX_ACTIVE_CELLS = 60


def _exact(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError("Allocation inputs must be Decimal")
    return exact_decimal(value)


def _money(value: Decimal) -> Decimal:
    return value.quantize(SCALE, rounding=ROUND_DOWN)


def _fmt(value: Decimal) -> str:
    return format(value, "f")


