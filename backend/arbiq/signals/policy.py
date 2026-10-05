"""Deterministic independent alert gates over immutable observed evaluations."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

EDMONTON = ZoneInfo("America/Edmonton")


def timestamp(value):
    if isinstance(value, datetime):
        return value
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


