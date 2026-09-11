"""Typed paper-accounting values. No exchange execution or AI inputs exist here."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import wraps
from inspect import iscoroutinefunction
from uuid import UUID

from arbiq.finance.contracts import exact_decimal, utc_timestamp

ACCOUNTING_VERSION = "quantix-accounting-v1.0.0"
ZERO = Decimal(0)
SCALE = Decimal("0.000000000000000001")


def stored(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Accounting values require finite Decimal arithmetic")
    with localcontext() as ctx:
        ctx.prec = 78
        result = value.quantize(SCALE, rounding=ROUND_HALF_EVEN)
    return exact_decimal(result)


def positive(value: Decimal, name: str = "quantity", *, zero: bool = False) -> Decimal:
    value = exact_decimal(value)
    if value < 0 or (value == 0 and not zero):
        raise ValueError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class Reservation:
    exchange_id: UUID
    asset_id: UUID
    quantity: Decimal

    def __post_init__(self):
        positive(self.quantity)


@dataclass(frozen=True, slots=True)
class LegAccounting:
    journal_id: UUID
    duplicate: bool
    base_change: Decimal
    quote_change: Decimal
    cost_basis_removed_cad: Decimal | None
    realized_trading_cad: Decimal | None
    realized_fx_cad: Decimal | None
    fees_cad: Decimal | None


@dataclass(frozen=True, slots=True)
class PriceEvidence:
    cad_per_unit: Decimal | None
    observed_at: datetime | None
    received_at: datetime | None
    verified: bool = False
    source: str = "unavailable"
    provenance: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.cad_per_unit is not None:
            if (
                not isinstance(self.cad_per_unit, Decimal)
                or not self.cad_per_unit.is_finite()
                or self.cad_per_unit <= 0
            ):
                raise ValueError("Valuation rate requires positive finite Decimal")
        for value in (self.observed_at, self.received_at):
            if value is not None:
                utc_timestamp(value)


class AccountingError(ValueError):
    """A financial invariant failed; the transaction must be rolled back."""


class InsufficientBalance(AccountingError):
    pass


def decimal_context(function):
    """Private Decimal context survives await points without changing other tasks."""
    if iscoroutinefunction(function):

        @wraps(function)
        async def asynchronous(*args, **kwargs):
            with localcontext() as ctx:
                ctx.prec = 78
                return await function(*args, **kwargs)

        return asynchronous

    @wraps(function)
    def synchronous(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = 78
            return function(*args, **kwargs)

    return synchronous
