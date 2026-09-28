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


