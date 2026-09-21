"""Pure Decimal depth calculations. No networking, clock reads, orders or ledger writes."""

import heapq
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from fractions import Fraction
from functools import wraps

from arbiq.finance.contracts import exact_decimal, utc_timestamp
from arbiq.market_data.contracts import BookLevel
from arbiq.opportunities.contracts import (
    DEFAULT_BUDGETS,
    BudgetResult,
    ConversionLeg,
    ConversionLevel,
    ConversionRoute,
    Costs,
    Evaluation,
    MarketInput,
    OptimizationResult,
    TransferAssumptions,
    TransferScenario,
)

D = Decimal
ZERO = D(0)
ONE = D(1)
DEFAULT_COSTS = Costs()


def precision[T](function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 78
            return function(*args, **kwargs)

    return wrapped


class InsufficientDepth(ValueError):
    """The visible evidence cannot satisfy the requested quantity."""


@precision
def fill_depth(levels: tuple[BookLevel | ConversionLevel, ...], quantity: Decimal) -> Decimal:
    """Return exact notional, consuming best-to-worst levels without extrapolation."""
    if not isinstance(quantity, D) or not quantity.is_finite() or quantity < 0:
        raise ValueError("Quantity must be a nonnegative Decimal")
    remaining, notional = quantity, ZERO
    for level in levels:
        take = min(remaining, level.quantity)
        notional += take * level.price
        remaining -= take
        if remaining == 0:
            return notional
    if remaining:
        raise InsufficientDepth("INSUFFICIENT_OBSERVED_DEPTH")
    return notional


@precision
def quantity_for_notional(
    levels: tuple[BookLevel | ConversionLevel, ...],
    notional: Decimal,
) -> Decimal:
    """Largest visible base quantity affordable by the given quote notional."""
    if not isinstance(notional, D) or not notional.is_finite() or notional < 0:
        raise ValueError("Notional must be a nonnegative Decimal")
    remaining, quantity = notional, ZERO
    for level in levels:
        take = min(level.quantity, remaining / level.price)
        quantity += take
        remaining -= take * level.price
        if remaining <= 0:
            break
    return quantity


@precision
def conversion_leg_from_book(
    base_currency: str,
    quote_currency: str,
    *,
    bids: tuple[BookLevel, ...],
    asks: tuple[BookLevel, ...],
    reverse: bool = False,
    fee_rate: Decimal = ZERO,
    evidence: str = "",
) -> ConversionLeg:
    """One executable direction, retaining depth rather than assigning stablecoin parity."""
    if reverse:
        return ConversionLeg(
            quote_currency,
            base_currency,
            tuple(ConversionLevel(ONE / x.price, x.quantity * x.price) for x in asks),
            fee_rate,
            evidence,
        )
    return ConversionLeg(base_currency, quote_currency, bids, fee_rate, evidence)


@precision
def conversion_capacity(route: ConversionRoute) -> Decimal | None:
    """Largest executable source quantity through every hop; None means CAD identity."""
    capacity: Decimal | None = None
    for index, leg in enumerate(route.legs):
        local_capacity = sum((level.quantity for level in leg.levels), ZERO)
        try:
            in_source = required_input(ConversionRoute(route.legs[:index]), local_capacity)
        except InsufficientDepth:
            continue  # An earlier hop already limits the route more tightly.
        capacity = in_source if capacity is None else min(capacity, in_source)
    return capacity


@precision
def convert(route: ConversionRoute, amount: Decimal) -> Decimal:
    """Sell source units through executable depth; deduct each target fee once."""
    result = amount
    for leg in route.legs:
        result = fill_depth(leg.levels, result) * (ONE - leg.fee_rate)
    return result


@precision
def required_input(route: ConversionRoute, target_amount: Decimal) -> Decimal:
    """Exact source acquisition cost for target units, using the opposite FX side."""
    if not isinstance(target_amount, D) or not target_amount.is_finite() or target_amount < 0:
        raise ValueError("Target amount must be a nonnegative Decimal")
    result = target_amount
    for leg in reversed(route.legs):
        gross_target = result / (ONE - leg.fee_rate)
        capacity = sum((x.price * x.quantity for x in leg.levels), ZERO)
        if gross_target > capacity:
            raise InsufficientDepth("INSUFFICIENT_FX_DEPTH")
        result = quantity_for_notional(leg.levels, gross_target)
    return result


def _floor(value: Decimal, step: Decimal | None) -> Decimal:
    return value if step is None else (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil(value: Decimal, step: Decimal | None) -> Decimal:
    return (
        value if step is None else (value / step).to_integral_value(rounding=ROUND_CEILING) * step
    )


def _base_buy_factor(market: MarketInput) -> Decimal:
    return ONE - market.fee.rate if market.fee.currency == "base" else ONE


def _base_sell_factor(market: MarketInput) -> Decimal:
    return ONE + market.fee.rate if market.fee.currency == "base" else ONE


def _check_limits(market: MarketInput, quantity: Decimal, notional: Decimal) -> None:
    rules = market.rules
    if rules.min_quantity and quantity < rules.min_quantity:
        raise ValueError("BELOW_MINIMUM_QUANTITY")
    if rules.min_notional and notional < rules.min_notional:
        raise ValueError("BELOW_MINIMUM_NOTIONAL")
    if rules.max_quantity and quantity > rules.max_quantity:
        raise ValueError("ABOVE_MAXIMUM_QUANTITY")
    if rules.max_notional and notional > rules.max_notional:
        raise ValueError("ABOVE_MAXIMUM_NOTIONAL")


def _compatible(buy: MarketInput, sell: MarketInput) -> None:
    if buy.venue == sell.venue:
        raise ValueError("DUPLICATE_LIQUIDITY_VENUE")
    if buy.base_currency != sell.base_currency:
        raise ValueError("INCOMPATIBLE_BASE_IDENTITY")


@precision
def evaluate_route(
    buy: MarketInput,
    sell: MarketInput,
    requested_buy_quantity: Decimal,
    *,
    now: datetime,
    costs: Costs = DEFAULT_COSTS,
) -> Evaluation:
    """Project pre-funded inventory changes; no balances, fills or profit realization.

    Buy quantity is rounded DOWN. Base buy fees reduce acquired inventory; base sell
    fees increase the seller's required inventory. Selling is rounded DOWN again.
    Residual acquired base is retained without assigning it fabricated cash proceeds.
    """
    _compatible(buy, sell)
    utc_timestamp(now)
    exact_decimal(requested_buy_quantity)
    quantity = _floor(requested_buy_quantity, buy.rules.amount_step)
    if quantity <= 0:
        raise ValueError("NO_EXECUTABLE_QUANTITY")
    buy_notional = fill_depth(buy.asks, quantity)
    acquired = quantity * _base_buy_factor(buy)
    sell_quantity = _floor(acquired / _base_sell_factor(sell), sell.rules.amount_step)
    if sell_quantity <= 0:
        raise ValueError("NO_EXECUTABLE_SELL_QUANTITY")
    sell_notional = fill_depth(sell.bids, sell_quantity)
    _check_limits(buy, quantity, buy_notional)
    _check_limits(sell, sell_quantity, sell_notional)
    buy_fee = (quantity if buy.fee.currency == "base" else buy_notional) * buy.fee.rate
    sell_fee = (sell_quantity if sell.fee.currency == "base" else sell_notional) * sell.fee.rate
    buy_quote = buy_notional + (buy_fee if buy.fee.currency == "quote" else ZERO)
    sell_quote = sell_notional - (sell_fee if sell.fee.currency == "quote" else ZERO)
    sell_base = sell_quantity * _base_sell_factor(sell)
    reasons: list[str] = []
    for side, market in (("BUY", buy), ("SELL", sell)):
        if not market.fee.verified:
            reasons.append(f"{side}_FEE_UNVERIFIED")
        if not market.rules.verified or market.rules.amount_step is None:
            reasons.append(f"{side}_MARKET_LIMITS_UNVERIFIED")
    if not costs.verified:
        reasons.append("REBALANCING_AND_SLIPPAGE_ASSUMPTIONS_UNVERIFIED")
    acquisition = proceeds = gross = fees = fx_cost = expenses = net = net_pct = None
    slippage = rebalancing = None
    buy_fee_cad = sell_fee_cad = base_fee_value = None
    fx_usable = True
    for side, market in (("BUY", buy), ("SELL", sell)):
        if market.fx is None:
            reasons.append(f"{side}_CAD_CONVERSION_UNAVAILABLE")
            fx_usable = False
        elif market.fx.fresh_until < now:
            reasons.append(f"{side}_CAD_CONVERSION_STALE")
            fx_usable = False
        elif any(
            timestamp is not None and timestamp > now
            for timestamp in (market.fx.observed_at, market.fx.available_at)
        ):
            reasons.append(f"{side}_CAD_CONVERSION_NOT_YET_AVAILABLE")
            fx_usable = False
        elif not market.fx.verified:
            reasons.append(f"{side}_CAD_CONVERSION_UNVERIFIED")
    if fx_usable:
        try:
            acquisition = required_input(buy.fx.from_cad, buy_quote)
            proceeds = convert(sell.fx.to_cad, sell_quote)
            gross_acquisition = required_input(buy.fx.from_cad, buy_notional)
            gross_proceeds = convert(sell.fx.to_cad, sell_notional)
            # Base fees have already affected acquired/sold quantities. Value them
            # at the corresponding leg VWAP for fee disclosure and add them back
            # only to the defined pre-fee gross figure, never to net cash proceeds.
            buy_base_value = (
                gross_acquisition * buy.fee.rate if buy.fee.currency == "base" else ZERO
            )
            sell_base_value = (
                gross_proceeds * sell.fee.rate if sell.fee.currency == "base" else ZERO
            )
            buy_fee_cad = acquisition - gross_acquisition + buy_base_value
            sell_fee_cad = gross_proceeds - proceeds + sell_base_value
            base_fee_value = buy_base_value + sell_base_value
            fees = buy_fee_cad + sell_fee_cad
            gross = gross_proceeds - gross_acquisition + base_fee_value
            slippage = acquisition * costs.slippage_bps / D(10000)
            rebalancing = acquisition * costs.rebalancing_bps / D(10000)
            rebalancing += costs.fixed_rebalancing_cad
            expenses = slippage + rebalancing
            net = proceeds - acquisition - expenses
            net_pct = net / acquisition * D(100)
            # Round-trip conversion drag is separately observable, already included
            # in cashflows above; never subtract it again. Zero CAD identity is real.
        except InsufficientDepth:
            acquisition = proceeds = gross = fees = fx_cost = expenses = net = net_pct = None
            slippage = rebalancing = None
            buy_fee_cad = sell_fee_cad = base_fee_value = None
            reasons.append("INSUFFICIENT_EXECUTABLE_FX_DEPTH")
        if acquisition is not None:
            try:
                buy_roundtrip = convert(buy.fx.to_cad, buy_quote)
                sell_replacement = required_input(sell.fx.from_cad, sell_quote)
                fx_cost = (acquisition - buy_roundtrip + sell_replacement - proceeds) / D(2)
            except InsufficientDepth:
                pass  # Optional spread diagnostic must not invalidate executable cashflows.
    quote_net = None
    if buy.quote_currency == sell.quote_currency:
        # Native quote result excludes CAD-denominated fixed rebalancing expense.
        quote_net = sell_quote - buy_quote
        quote_net -= buy_quote * (costs.slippage_bps + costs.rebalancing_bps) / D(10000)
        if costs.fixed_rebalancing_cad and buy.quote_currency == "CAD":
            quote_net -= costs.fixed_rebalancing_cad
        elif costs.fixed_rebalancing_cad:
            reasons.append("NATIVE_PNL_EXCLUDES_FIXED_CAD_EXPENSE")
    if buy.quote_currency != sell.quote_currency and not fx_usable:
        reasons.append("CROSS_QUOTE_COMPARISON_REQUIRES_FX")
    return Evaluation(
        buy_quantity=quantity,
        base_acquired=acquired,
        sell_quantity=sell_quantity,
        sell_base_required=sell_base,
        residual_base=acquired - sell_base,
        buy_vwap=buy_notional / quantity,
        sell_vwap=sell_notional / sell_quantity,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        buy_fee_amount=buy_fee,
        sell_fee_amount=sell_fee,
        buy_quote_required=buy_quote,
        sell_quote_received=sell_quote,
        acquisition_cost_cad=acquisition,
        sale_proceeds_cad=proceeds,
        gross_profit_cad=gross,
        fees_cad=fees,
        fx_cost_cad=fx_cost,
        expenses_cad=expenses,
        net_profit_cad=net,
        net_return_pct=net_pct,
        net_profit_quote=quote_net,
        verified=not reasons and net is not None,
        reasons=tuple(reasons),
        capital_requirements={
            buy.venue: {buy.quote_currency: buy_quote},
            sell.venue: {sell.base_currency: sell_base},
        },
        inventory_projection={
            buy.venue: {buy.quote_currency: -buy_quote, buy.base_currency: acquired},
            sell.venue: {sell.base_currency: -sell_base, sell.quote_currency: sell_quote},
        },
        slippage_reserve_cad=slippage,
        rebalancing_cost_cad=rebalancing,
        buy_fee_cad=buy_fee_cad,
        sell_fee_cad=sell_fee_cad,
        base_fee_value_cad=base_fee_value,
    )


@precision
def maximum_quantity(buy: MarketInput, sell: MarketInput) -> Decimal:
    _compatible(buy, sell)
    buy_depth = sum((level.quantity for level in buy.asks), ZERO)
    sell_depth = sum((level.quantity for level in sell.bids), ZERO)
    if buy.rules.max_quantity:
        buy_depth = min(buy_depth, buy.rules.max_quantity)
    if sell.rules.max_quantity:
        sell_depth = min(sell_depth, sell.rules.max_quantity)
    if buy.rules.max_notional:
        buy_depth = min(buy_depth, quantity_for_notional(buy.asks, buy.rules.max_notional))
    if sell.rules.max_notional:
        sell_depth = min(sell_depth, quantity_for_notional(sell.bids, sell.rules.max_notional))
    return _floor(
        min(buy_depth, sell_depth * _base_sell_factor(sell) / _base_buy_factor(buy)),
        buy.rules.amount_step,
    )


def _breakpoints(buy: MarketInput, sell: MarketInput, maximum: Decimal) -> set[Decimal]:
    points = {maximum, min(_minimum_quantity(buy, sell), maximum)}
    cumulative = ZERO
    for level in buy.asks:
        cumulative += level.quantity
        points.add(min(cumulative, maximum))
    cumulative = ZERO
    for level in sell.bids:
        cumulative += level.quantity
        points.add(min(cumulative * _base_sell_factor(sell) / _base_buy_factor(buy), maximum))
    for market, is_buy in ((buy, True), (sell, False)):
        for value in (market.rules.min_quantity, market.rules.max_quantity):
            if value:
                points.add(
                    min(
                        value
                        if is_buy
                        else value * _base_sell_factor(sell) / _base_buy_factor(buy),
                        maximum,
                    )
                )
        if market.rules.min_notional:
            value = quantity_for_notional(
                market.asks if is_buy else market.bids, market.rules.min_notional
            )
            points.add(
                min(
                    value if is_buy else value * _base_sell_factor(sell) / _base_buy_factor(buy),
                    maximum,
                )
            )
    return points


def _minimum_quantity(buy: MarketInput, sell: MarketInput) -> Decimal:
    """First buy quantity that can satisfy both legs' amount/notional minimums."""
    min_sell = sell.rules.min_quantity or ZERO
    if sell.rules.min_notional:
        min_sell = max(min_sell, quantity_for_notional(sell.bids, sell.rules.min_notional))
    min_sell = _ceil(min_sell, sell.rules.amount_step)
    minimum = max(
        buy.rules.min_quantity or ZERO, min_sell * _base_sell_factor(sell) / _base_buy_factor(buy)
    )
    if buy.rules.min_notional:
        minimum = max(minimum, quantity_for_notional(buy.asks, buy.rules.min_notional))
    minimum = _ceil(minimum, buy.rules.amount_step)
    if minimum == 0 and buy.rules.amount_step:
        minimum = buy.rules.amount_step
    return minimum


def _candidate_quantities(buy: MarketInput, sell: MarketInput, maximum: Decimal) -> set[Decimal]:
    points = _breakpoints(buy, sell, maximum)
    buy_step, sell_step = buy.rules.amount_step, sell.rules.amount_step
    if buy_step and maximum / buy_step <= 32:
        # Exact exhaustive lattice evaluation for small discrete universes.
        points.update(buy_step * n for n in range(1, int(maximum / buy_step) + 1))
    if buy_step and sell_step:
        # Seed the first exact inventory alignment. Otherwise a loss-making tiny
        # order with a large rounding residual can conceal a better slightly larger
        # order. Fractions here represent exact Decimal ratios, never float money.
        ratio = (
            Fraction(buy_step)
            * Fraction(_base_buy_factor(buy))
            / (Fraction(sell_step) * Fraction(_base_sell_factor(sell)))
        )
        alignment = buy_step * ratio.denominator
        aligned_minimum = _ceil(_minimum_quantity(buy, sell), alignment)
        if ZERO < aligned_minimum <= maximum:
            points.add(aligned_minimum)
    result: set[Decimal] = set()
    for point in points:
        for value in (_floor(point, buy_step), _ceil(point, buy_step)):
            if ZERO < value <= maximum:
                result.add(value)
        if sell_step:
            sell_qty = point * _base_buy_factor(buy) / _base_sell_factor(sell)
            for count in (_floor(sell_qty, sell_step), _ceil(sell_qty, sell_step)):
                value = _ceil(count * _base_sell_factor(sell) / _base_buy_factor(buy), buy_step)
                if ZERO < value <= maximum:
                    result.add(value)
    return result


def _loss_candidates(
    buy: MarketInput,
    sell: MarketInput,
    maximum: Decimal,
    costs: Costs,
) -> set[Decimal] | None:
    """Prove a decreasing continuous envelope before evaluating irrelevant depth.

    Rounding can still favor a slightly larger aligned order. Those outcomes are
    found/certified by the same exact branch-and-bound, never assumed away.
    """
    step = buy.rules.amount_step
    if step is None or maximum / step <= 32 or buy.fx is None or sell.fx is None:
        return None
    minimum = _minimum_quantity(buy, sell)
    if minimum <= 0 or minimum > maximum:
        return None
    try:
        _, slope = _continuous_net_slope(buy, sell, minimum, costs, left=False)
    except InsufficientDepth:
        return None
    if slope > 0:
        return None
    points = {minimum}
    if minimum + step <= maximum:
        points.add(minimum + step)
    if sell.rules.amount_step:
        ratio = (
            Fraction(step)
            * Fraction(_base_buy_factor(buy))
            / (Fraction(sell.rules.amount_step) * Fraction(_base_sell_factor(sell)))
        )
        aligned = _ceil(minimum, step * ratio.denominator)
        if aligned <= maximum:
            points.add(aligned)
    return points


def _leg_marginal(leg: ConversionLeg, amount: Decimal, *, left: bool) -> Decimal:
    cumulative = ZERO
    for level in leg.levels:
        cumulative += level.quantity
        if amount < cumulative or (left and amount == cumulative):
            return level.price * (ONE - leg.fee_rate)
    # At the capacity endpoint only the left derivative is defined.
    if amount == cumulative:
        return leg.levels[-1].price * (ONE - leg.fee_rate)
    raise InsufficientDepth("INSUFFICIENT_EXECUTABLE_FX_DEPTH")


