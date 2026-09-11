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


