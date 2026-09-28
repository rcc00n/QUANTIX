"""Exact portfolio marks and no-trading benchmark, with missing-price propagation."""

from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select

from arbiq.db.models import Asset, Exchange
from arbiq.market_data.quality import safe_json
from arbiq.portfolio.accounting import lock_session, reconcile
from arbiq.portfolio.contracts import ACCOUNTING_VERSION, decimal_context, stored
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint, PortfolioPosition

ZERO = Decimal(0)


def complete_sum(values):
    values = list(values)
    if any(v is None for v in values):
        return None
    with localcontext() as ctx:
        ctx.prec = 78
        return stored(sum(values, ZERO))


def valid_price(price, now, maximum_age_seconds, *, cad=False):
    if cad:
        return Decimal(1), True, None
    if price is None or price.cad_per_unit is None:
        return None, False, "CAD_PRICE_UNAVAILABLE"
    if price.observed_at is None or price.received_at is None:
        return None, False, "VALUATION_TIMESTAMP_UNAVAILABLE"
    for timestamp in (price.observed_at, price.received_at):
        age = (now - timestamp).total_seconds()
        if age < 0:
            return None, False, "VALUATION_EVIDENCE_FROM_FUTURE"
        if age > maximum_age_seconds:
            return None, False, "VALUATION_EVIDENCE_STALE"
    return price.cad_per_unit, price.verified, None


