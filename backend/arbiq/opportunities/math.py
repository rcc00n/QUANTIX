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


class InsufficientDepth(ValueError):
    """The visible evidence cannot satisfy the requested quantity."""


@precision
def fill_depth(levels: tuple[BookLevel | ConversionLevel, ...], quantity: Decimal) -> Decimal:
    """Return exact notional, consuming best-to-worst levels without extrapolation."""
    if not isinstance(quantity, D) or not quantity.is_finite() or quantity < 0:
        raise ValueError("Quantity must be a nonnegative Decimal")
    remaining, notional = quantity, ZERO
    for level in levels:
        take = min(remaining, level.quantity)
        notional += take * level.price
        remaining -= take
        if remaining == 0:
            return notional
    if remaining:
        raise InsufficientDepth("INSUFFICIENT_OBSERVED_DEPTH")
    return notional


@precision
def quantity_for_notional(
    levels: tuple[BookLevel | ConversionLevel, ...],
    notional: Decimal,
) -> Decimal:
    """Largest visible base quantity affordable by the given quote notional."""
    if not isinstance(notional, D) or not notional.is_finite() or notional < 0:
        raise ValueError("Notional must be a nonnegative Decimal")
    remaining, quantity = notional, ZERO
    for level in levels:
        take = min(level.quantity, remaining / level.price)
        quantity += take
        remaining -= take * level.price
        if remaining <= 0:
            break
    return quantity


@precision
def conversion_leg_from_book(
    base_currency: str,
    quote_currency: str,
    *,
    bids: tuple[BookLevel, ...],
    asks: tuple[BookLevel, ...],
    reverse: bool = False,
    fee_rate: Decimal = ZERO,
    evidence: str = "",
) -> ConversionLeg:
    """One executable direction, retaining depth rather than assigning stablecoin parity."""
    if reverse:
        return ConversionLeg(
            quote_currency,
            base_currency,
            tuple(ConversionLevel(ONE / x.price, x.quantity * x.price) for x in asks),
            fee_rate,
            evidence,
        )
    return ConversionLeg(base_currency, quote_currency, bids, fee_rate, evidence)


@precision
def conversion_capacity(route: ConversionRoute) -> Decimal | None:
    """Largest executable source quantity through every hop; None means CAD identity."""
    capacity: Decimal | None = None
    for index, leg in enumerate(route.legs):
        local_capacity = sum((level.quantity for level in leg.levels), ZERO)
        try:
            in_source = required_input(ConversionRoute(route.legs[:index]), local_capacity)
        except InsufficientDepth:
            continue  # An earlier hop already limits the route more tightly.
        capacity = in_source if capacity is None else min(capacity, in_source)
    return capacity


