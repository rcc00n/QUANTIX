"""Deterministic single-leg fills from observed depth. Never an exchange client."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from arbiq.finance.contracts import exact_decimal
from arbiq.market_data.quality import CapturedBook, DataState
from arbiq.opportunities.contracts import Evaluation, Fee, MarketRules
from arbiq.opportunities.math import fill_depth, quantity_for_notional

VERSION = "paper-depth-v1.2.0"
UNIT = Decimal("1e-18")


@dataclass(frozen=True)
class LegFill:
    requested_quantity: Decimal
    quantity: Decimal
    observed_notional: Decimal
    quote_notional: Decimal
    observed_vwap: Decimal
    modeled_price: Decimal
    fee_quantity: Decimal
    fee_currency: str
    slippage_quote: Decimal
    partial: bool


