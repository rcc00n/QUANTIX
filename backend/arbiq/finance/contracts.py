"""Shared strict values. JSON financial inputs must be decimal strings, never floats."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field


def exact_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Use a decimal string; binary floating point is forbidden")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("Decimal must be finite")
    if result.copy_abs() >= Decimal("1e20") or result.as_tuple().exponent < -18:
        raise ValueError("Decimal exceeds NUMERIC(38,18) capacity")
    return result


def utc_timestamp(value: datetime) -> datetime:
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("Timestamp must have an explicit UTC offset")
    return value


ExactDecimal = Annotated[Decimal, BeforeValidator(exact_decimal)]
PositiveDecimal = Annotated[ExactDecimal, Field(gt=0)]
NonNegativeDecimal = Annotated[ExactDecimal, Field(ge=0)]
UTCTimestamp = Annotated[datetime, AfterValidator(utc_timestamp)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
