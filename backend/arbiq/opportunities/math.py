"""Pure Decimal depth calculations. No networking, clock reads, orders or ledger writes."""

import heapq
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from fractions import Fraction
from functools import wraps

from arbiq.finance.contracts import exact_decimal, utc_timestamp
from arbiq.market_data.contracts import BookLevel
from arbiq.opportunities.contracts import (
    DEFAULT_BUDGETS,
    BudgetResult,
    ConversionLeg,
    ConversionLevel,
    ConversionRoute,
    Costs,
    Evaluation,
    MarketInput,
    OptimizationResult,
    TransferAssumptions,
    TransferScenario,
)

D = Decimal
ZERO = D(0)
ONE = D(1)
DEFAULT_COSTS = Costs()


def precision[T](function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 78
            return function(*args, **kwargs)

    return wrapped


