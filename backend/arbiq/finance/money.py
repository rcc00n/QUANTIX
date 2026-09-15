"""Deterministic primitives, not a trading/simulation engine."""

from decimal import ROUND_DOWN, Decimal, localcontext

from arbiq.finance.contracts import exact_decimal

CALCULATION_VERSION = "foundation-v1"


def convert_to_cad(amount: Decimal, cad_per_unit: Decimal) -> Decimal:
    amount, cad_per_unit = exact_decimal(amount), exact_decimal(cad_per_unit)
    if cad_per_unit <= 0:
        raise ValueError("FX rate must be positive")
    with localcontext() as ctx:
        ctx.prec = 78
        return amount * cad_per_unit


def round_quantity_down(quantity: Decimal, step: Decimal) -> Decimal:
    quantity, step = exact_decimal(quantity), exact_decimal(step)
    if quantity < 0 or step <= 0:
        raise ValueError("Quantity must be nonnegative and step must be positive")
    with localcontext() as ctx:
        ctx.prec = 78
        return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


