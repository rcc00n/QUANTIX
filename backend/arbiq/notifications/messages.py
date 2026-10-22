"""Bounded plain-text messages from exact observed values, never AI-generated claims."""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from arbiq.signals.policy import amount, timestamp


def value(v):
    number = amount(v)
    return format(number, "f") if number is not None else "unavailable"


def cad(v):
    return display_value(v, places=2, minimum_places=2, currency="C$")


