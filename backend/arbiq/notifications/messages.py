"""Bounded plain-text messages from exact observed values, never AI-generated claims."""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from arbiq.signals.policy import amount, timestamp


def value(v):
    number = amount(v)
    return format(number, "f") if number is not None else "unavailable"


def display_value(v, *, places=8, minimum_places=0, currency=""):
    """Compact display only. Rounding never feeds a rule, journal or evidence field.

    Tiny nonzero values use six significant digits in scientific notation rather
    than appearing as zero. An approximation marker identifies any lost precision.
    """
    number = amount(v)
    if number is None:
        return "unavailable"
    quantum = Decimal(1).scaleb(-places)
    if number != 0 and (abs(number) < quantum or number.adjusted() >= 15):
        mantissa, exponent = format(number, ".5E").split("E")
        rendered = f"{mantissa.rstrip('0').rstrip('.')}e{int(exponent)}"
        rounded = Decimal(rendered)
    else:
        with localcontext() as context:
            context.prec = max(32, len(number.as_tuple().digits) + places + 2)
            rounded = number.quantize(quantum, rounding=ROUND_HALF_EVEN)
        rendered = format(rounded, "f")
        if "." in rendered:
            whole, fractional = rendered.split(".", 1)
            fractional = fractional.rstrip("0").ljust(minimum_places, "0")
            rendered = whole + ("." + fractional if fractional else "")
    marker = "≈" if rounded != number else ""
    return marker + currency + rendered


def cad(v):
    return display_value(v, places=2, minimum_places=2, currency="C$")


