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


