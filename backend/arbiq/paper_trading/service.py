"""Authenticated portfolio lifecycle helpers; callers own commits."""

from decimal import Decimal, localcontext

from sqlalchemy import func, select

from arbiq.db.models import Asset, Exchange, PaperSession, User
from arbiq.finance.contracts import Contract, PositiveDecimal
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.accounting import fund
from arbiq.portfolio.contracts import decimal_context, stored
from arbiq.settings.strategy import save_strategy


class Allocation(Contract):
    exchange: str
    asset: str = "fiat:CAD"
    quantity: PositiveDecimal
    cad_rate: PositiveDecimal | None = None
    evidence: dict = {}


