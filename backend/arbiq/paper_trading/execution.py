"""Deterministic single-leg fills from observed depth. Never an exchange client."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from arbiq.finance.contracts import exact_decimal
from arbiq.market_data.quality import CapturedBook, DataState
from arbiq.opportunities.contracts import Evaluation, Fee, MarketRules
from arbiq.opportunities.conversions import capture_uncertainty_ms
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
    if rules.observed_at is not None and rules.observed_at > book.received_at:
        raise ValueError("MARKET_RULES_NOT_YET_AVAILABLE")
    if rules.market_order_available is False:
        raise ValueError("MARKET_ORDER_UNAVAILABLE")
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
        if (
            rules.version
            and rules.price_tick
            and any(level.price % rules.price_tick for level in levels)
        ):
            raise ValueError("PRICE_CONFLICTS_WITH_MARKET_TICK")
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


def recheck_projection(
    *,
    buy_book,
    sell_book,
    buy_input,
    sell_input,
    planned_quantity,
    decision_at,
    strategy,
    captured_fx=None,
):
    """Approval before the first leg uses only information available at that time."""
    from arbiq.opportunities.analysis import dump_input, load_input
    from arbiq.opportunities.contracts import Costs
    from arbiq.opportunities.math import evaluate_route, maximum_quantity
    from arbiq.settings.strategy import minimum_buy_satisfied

    with localcontext() as ctx:
        ctx.prec = 78
        if buy_book is None or sell_book is None:
            raise ValueError("NO_CAUSAL_OPPOSITE_BOOK")
        for book in (buy_book, sell_book):
            good, reason = evidence_eligibility(
                book,
                now=decision_at,
                eligible_at=book.received_at,
                maximum_age=strategy.maximum_book_age_seconds,
                research=strategy.mode == "RESEARCH_SHADOW",
                allow_missing=strategy.allow_missing_source_timestamp,
                minimum_confidence=strategy.minimum_confidence,
            )
            if not good:
                raise ValueError(reason)
        if strategy.strategy_lab_profile:
            uncertainty = sum(
                capture_uncertainty_ms(book.verification.get("clock") or {})
                for book in (buy_book, sell_book)
            )
            skew_limit = timedelta(seconds=10, milliseconds=-uncertainty)
            if (
                abs(buy_book.received_at - sell_book.received_at) > skew_limit
                or abs(buy_book.exchange_timestamp - sell_book.exchange_timestamp) > skew_limit
            ):
                raise ValueError("STRATEGY_LAB_CROSS_BOOK_SKEW")
        if buy_book.market_id != __import__("uuid").UUID(
            buy_input.market_id
        ) or sell_book.market_id != __import__("uuid").UUID(sell_input.market_id):
            raise ValueError("MARKET_IDENTITY_MISMATCH")

        def update(m, b):
            raw = dump_input(m)
            raw.update(
                asks=[x.model_dump(mode="json") for x in b.normalized.asks],
                bids=[x.model_dump(mode="json") for x in b.normalized.bids],
            )
            if captured_fx and m.quote_currency in captured_fx:
                raw["fx"] = captured_fx[m.quote_currency]
            return load_input(raw)

        buy, sell = update(buy_input, buy_book), update(sell_input, sell_book)
        q = min(planned_quantity, maximum_quantity(buy, sell))
        result = evaluate_route(
            buy,
            sell,
            q,
            now=decision_at,
            costs=Costs(slippage_bps=strategy.slippage_bps, rebalancing_bps=strategy.rebalance_bps),
        )
        if result.net_profit_cad is None:
            raise ValueError("CAD_RECHECK_VALUATION_UNAVAILABLE")
        if not minimum_buy_satisfied(strategy, result.acquisition_cost_cad):
            raise ValueError("MINIMUM_BUY_CAPITAL")
        allowed = [
            result.net_profit_cad >= strategy.minimum_profit_cad,
            result.net_return_pct >= strategy.minimum_profit_pct,
        ]
        if (
            not (all(allowed) if strategy.threshold_logic == "AND" else any(allowed))
            or result.net_profit_cad < -strategy.maximum_loss_cad
        ):
            raise ValueError("SPREAD_DISAPPEARED")
        return result


def floor_derived(quantity: Decimal, step: Decimal) -> Decimal:
    """Round an internal78-digit intermediate before NUMERIC input validation."""
    exact_decimal(step)
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity < 0 or step <= 0:
        raise ValueError("Invalid derived quantity")
    with localcontext() as ctx:
        ctx.prec = 78
        return exact_decimal((quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step)
