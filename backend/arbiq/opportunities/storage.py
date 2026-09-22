"""Research persistence with immutable evidence, revisions and lifecycle transitions."""

from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import select

from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    FeeVersion,
    OrderBookSnapshot,
)
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.contracts import CALCULATION_VERSION

VERSION = CALCULATION_VERSION


def numeric(value):
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 78
        value = Decimal(value).quantize(Decimal("0.000000000000000001"))
        if not value.is_finite() or abs(value) >= Decimal("1e20"):
            raise ValueError("Financial evidence exceeds NUMERIC(38,18)")
        return value


