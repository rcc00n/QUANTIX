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


def amount(value):
    if isinstance(value, (bool, float)) or value is None:
        return None
    try:
        value = Decimal(value)
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def quiet_hours(now, policy):
    if not policy.quiet_hours_enabled or policy.quiet_start == policy.quiet_end:
        return False
    local_time = now.astimezone(EDMONTON).strftime("%H:%M")
    if policy.quiet_start < policy.quiet_end:
        return policy.quiet_start <= local_time < policy.quiet_end
    return local_time >= policy.quiet_start or local_time < policy.quiet_end


@dataclass(frozen=True)
class Gate:
    eligible: bool
    classification: str
    emergency: bool
    silent: bool
    reasons: tuple[str, ...]


