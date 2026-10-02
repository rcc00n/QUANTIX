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


def simulate_leg(
    book: CapturedBook,
    side: str,
    requested: Decimal,
    available: Decimal,
    fee: Fee,
    rules: MarketRules,
    slippage_bps: Decimal = Decimal(0),
    maximum_slippage_bps: Decimal | None = None,
) -> LegFill:
    with localcontext() as ctx:
        ctx.prec = 78
        requested, available, slippage_bps = map(
            exact_decimal, (requested, available, slippage_bps)
        )
        if (
            side not in {"buy", "sell"}
            or requested <= 0
            or available < 0
            or not 0 <= slippage_bps <= 10000
        ):
            raise ValueError("Invalid leg inputs")
        levels = book.normalized.asks if side == "buy" else book.normalized.bids
        quantity = min(requested, sum((x.quantity for x in levels), Decimal(0)))
        if rules.max_quantity:
            quantity = min(quantity, rules.max_quantity)
        if rules.max_notional:
            quantity = min(quantity, quantity_for_notional(levels, rules.max_notional))
        slip = slippage_bps / Decimal(10000)
        factor = Decimal(1) + slip if side == "buy" else Decimal(1) - slip
        if factor <= 0:
            raise ValueError("No sale proceeds after reserve")
        if rules.max_notional:
            quantity = min(quantity, quantity_for_notional(levels, rules.max_notional / factor))
        if side == "buy":
            # The paper ledger rounds the modeled principal upward before charging
            # fees. Both the budget and the principal are integer accounting units.
            # principal + ceil(principal*rate) <= budget is therefore equivalent
            # to principal <= floor(budget/(1+rate)) on that same grid.
            modeled_budget = (
                floor_derived(available / (1 + fee.rate), UNIT)
                if fee.currency == "quote"
                else available
            )
            quote_budget = modeled_budget / factor
            quantity = min(quantity, quantity_for_notional(levels, quote_budget))
        else:
            quantity = min(quantity, available / (1 + fee.rate if fee.currency == "base" else 1))
        quantity = floor_derived(quantity, rules.amount_step or UNIT)
        if quantity <= 0:
            raise ValueError("INSUFFICIENT_INVENTORY_OR_DEPTH")
        observed = fill_depth(levels, quantity)
        if rules.min_quantity and quantity < rules.min_quantity:
            raise ValueError("MINIMUM_QUANTITY")
        if rules.min_notional and observed < rules.min_notional:
            raise ValueError("MINIMUM_NOTIONAL")
        modeled = (observed * factor).quantize(
            UNIT, rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR
        )
        if maximum_slippage_bps is not None:
            limit = exact_decimal(maximum_slippage_bps)
            impact = (
                (modeled / quantity / levels[0].price - 1)
                if side == "buy"
                else (1 - modeled / quantity / levels[0].price)
            ) * 10000
            if not 0 <= limit <= 10000 or impact > limit:
                raise ValueError("MAXIMUM_SLIPPAGE_EXCEEDED")
        if rules.min_notional and modeled < rules.min_notional:
            raise ValueError("MINIMUM_NOTIONAL_AFTER_SLIPPAGE")
        charge = ((quantity if fee.currency == "base" else modeled) * fee.rate).quantize(
            UNIT, rounding=ROUND_CEILING
        )
        if modeled <= 0:
            raise ValueError("QUOTE_NOTIONAL_BELOW_ACCOUNTING_PRECISION")
        if side == "buy":
            spend = modeled + (charge if fee.currency == "quote" else 0)
            credit = quantity - (charge if fee.currency == "base" else 0)
        else:
            spend = quantity + (charge if fee.currency == "base" else 0)
            credit = modeled - (charge if fee.currency == "quote" else 0)
        if spend > available:
            raise ValueError("INSUFFICIENT_ROUNDED_RESERVATION")
        if credit <= 0:
            raise ValueError("FEE_CONSUMES_MODELED_PROCEEDS")
        return LegFill(
            requested,
            quantity,
            observed,
            modeled,
            observed / quantity,
            modeled / quantity,
            charge,
            fee.currency,
            abs(modeled - observed),
            quantity < requested,
        )


