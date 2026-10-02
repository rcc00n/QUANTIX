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


def evidence_eligibility(
    book: CapturedBook,
    *,
    now: datetime,
    eligible_at: datetime,
    maximum_age: int,
    research: bool,
    allow_missing: bool = False,
    clock_trusted: bool = True,
    minimum_confidence: str | None = None,
) -> tuple[bool, str]:
    if not clock_trusted:
        return False, "UTC_UNTRUSTED"
    if book.received_at < eligible_at:
        return False, "OBSERVATION_BEFORE_REACTION_DELAY"
    if not 0 <= (now - book.received_at).total_seconds() <= maximum_age:
        return False, "INGESTION_STALE_OR_FUTURE"
    if book.exchange_timestamp is None:
        if not research or not allow_missing:
            return False, "SOURCE_TIMESTAMP_UNAVAILABLE"
    elif not 0 <= (now - book.exchange_timestamp).total_seconds() <= maximum_age:
        return False, "SOURCE_STALE_OR_FUTURE"
    unsafe = {"collector_clock_jump", "symbol_mismatch", "last_request_failed"}
    if any(
        r in unsafe or r.startswith(("invalid_", "unordered_")) or "crossed" in r
        for r in book.reasons
    ):
        return False, "UNSAFE_BOOK_EVIDENCE"
    if book.state in {DataState.STALE, DataState.UNAVAILABLE}:
        return False, "BOOK_" + book.state.value
    minimum_confidence = minimum_confidence or ("ANOMALY_RESEARCH" if research else "HEALTHY")
    if minimum_confidence == "HEALTHY" and book.state != DataState.HEALTHY:
        return False, "CONFIDENCE_BELOW_STRATEGY_MINIMUM"
    if minimum_confidence == "DEGRADED" and book.state not in {
        DataState.HEALTHY,
        DataState.DEGRADED,
    }:
        return False, "CONFIDENCE_BELOW_STRATEGY_MINIMUM"
    if not research and book.state != DataState.HEALTHY:
        return False, "UNVERIFIED_BOOK"
    if not book.normalized.asks or not book.normalized.bids:
        return False, "MISSING_DEPTH"
    if book.normalized.bids[0].price >= book.normalized.asks[0].price:
        return False, "CROSSED_BOOK"
    return True, "RESEARCH_OBSERVED" if research else "VALIDATED_OBSERVED"


