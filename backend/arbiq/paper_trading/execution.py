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


def planned_reserves(
    evaluation: Evaluation, buy_fee: Fee, sell_fee: Fee, slippage_bps: Decimal
) -> tuple[Decimal, Decimal]:
    """Exact spendable units matching simulator debit/fee rounding at pinned depth."""
    with localcontext() as ctx:
        ctx.prec = 78
        if (
            evaluation.buy_notional <= 0
            or evaluation.sell_quantity <= 0
            or not 0 <= slippage_bps <= 10000
        ):
            raise ValueError("Invalid planned reservation")
        modeled_buy = (evaluation.buy_notional * (1 + slippage_bps / 10000)).quantize(
            UNIT, rounding=ROUND_CEILING
        )
        buy_charge = (modeled_buy * buy_fee.rate).quantize(UNIT, rounding=ROUND_CEILING)
        sell_charge = (evaluation.sell_quantity * sell_fee.rate).quantize(
            UNIT, rounding=ROUND_CEILING
        )
        return (
            exact_decimal(modeled_buy + (buy_charge if buy_fee.currency == "quote" else 0)),
            exact_decimal(
                evaluation.sell_quantity + (sell_charge if sell_fee.currency == "base" else 0)
            ),
        )


