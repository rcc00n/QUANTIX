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


def _convert_slope(
    route: ConversionRoute, amount: Decimal, *, left: bool
) -> tuple[Decimal, Decimal]:
    derivative = ONE
    for leg in route.legs:
        derivative *= _leg_marginal(leg, amount, left=left)
        amount = fill_depth(leg.levels, amount) * (ONE - leg.fee_rate)
    return amount, derivative


def _required_slope(
    route: ConversionRoute,
    amount: Decimal,
    *,
    left: bool,
) -> tuple[Decimal, Decimal]:
    derivative = ONE
    for leg in reversed(route.legs):
        gross = amount / (ONE - leg.fee_rate)
        capacity = sum((x.quantity * x.price for x in leg.levels), ZERO)
        if gross > capacity:
            raise InsufficientDepth("INSUFFICIENT_EXECUTABLE_FX_DEPTH")
        amount = quantity_for_notional(leg.levels, gross)
        derivative /= _leg_marginal(leg, amount, left=left)
    return amount, derivative


def _book_slope(levels: tuple[BookLevel, ...], quantity: Decimal, *, left: bool) -> Decimal:
    cumulative = ZERO
    for level in levels:
        cumulative += level.quantity
        if quantity < cumulative or (left and quantity == cumulative):
            return level.price
    if quantity == cumulative:
        return levels[-1].price
    raise InsufficientDepth("INSUFFICIENT_OBSERVED_DEPTH")


def _continuous_net_slope(
    buy: MarketInput,
    sell: MarketInput,
    quantity: Decimal,
    costs: Costs,
    *,
    left: bool,
) -> tuple[Decimal, Decimal]:
    """Concave no-rounding envelope, an upper bound on realizable rounded cashflows."""
    buy_quote_factor = ONE + buy.fee.rate if buy.fee.currency == "quote" else ONE
    sell_quote_factor = ONE - sell.fee.rate if sell.fee.currency == "quote" else ONE
    sale_factor = _base_buy_factor(buy) / _base_sell_factor(sell)
    sell_quantity = quantity * sale_factor
    buy_quote = fill_depth(buy.asks, quantity) * buy_quote_factor
    sell_quote = fill_depth(sell.bids, sell_quantity) * sell_quote_factor
    acquisition, cost_slope = _required_slope(buy.fx.from_cad, buy_quote, left=left)
    proceeds, sale_slope = _convert_slope(sell.fx.to_cad, sell_quote, left=left)
    reserve_factor = ONE + (costs.slippage_bps + costs.rebalancing_bps) / D(10000)
    slope = sale_slope * _book_slope(sell.bids, sell_quantity, left=left)
    slope *= sale_factor * sell_quote_factor
    buy_book_slope = _book_slope(buy.asks, quantity, left=left)
    slope -= cost_slope * buy_book_slope
    slope -= (reserve_factor - ONE) * cost_slope * buy_book_slope * buy_quote_factor
    # The acquisition derivative includes quote trading fees once.
    slope -= cost_slope * buy_book_slope * (buy_quote_factor - ONE)
    return proceeds - acquisition * reserve_factor - costs.fixed_rebalancing_cad, slope


def _interval_bound(
    low: Decimal,
    high: Decimal,
    low_tangent: tuple[Decimal, Decimal],
    high_tangent: tuple[Decimal, Decimal],
) -> Decimal:
    low_net, low_slope = low_tangent
    high_net, high_slope = high_tangent
    points = [low, high]
    if low_slope != high_slope:
        crossing = (high_net - low_net + low_slope * low - high_slope * high) / (
            low_slope - high_slope
        )
        if low < crossing < high:
            points.append(crossing)
    return max(
        min(low_net + low_slope * (q - low), high_net + high_slope * (q - high)) for q in points
    )


def _certify_optimum(
    buy: MarketInput,
    sell: MarketInput,
    capped: Decimal,
    budget: Decimal,
    now: datetime,
    costs: Costs,
    best: Evaluation | None,
    max_search_evaluations: int,
) -> tuple[Evaluation | None, int, bool]:
    """Bounded branch-and-bound on the exact buy quantity lattice.

    Ask acquisition costs are convex; bid proceeds are concave. Executable FX preserves
    that property. Tangent envelopes bound every unevaluated rounded order quantity.
    If the work limit is reached the result explicitly loses verified optimality.
    """
    step = buy.rules.amount_step
    if step is None:
        return best, 0, False
    count = int(capped / step)
    if count <= 32:
        return best, 0, True  # The initial pass enumerated the entire lattice.
    if best is None:
        return best, 0, False
    try:
        low_count = max(
            1, int((_minimum_quantity(buy, sell) / step).to_integral_value(rounding=ROUND_CEILING))
        )
        if low_count > count:
            return best, 0, True
        low_quantity = step * low_count
        low_tangent = _continuous_net_slope(buy, sell, low_quantity, costs, left=False)
        high_tangent = _continuous_net_slope(buy, sell, capped, costs, left=True)
        initial_bound = _interval_bound(low_quantity, capped, low_tangent, high_tangent)
        # Descendants use lattice quantities. Preserve the original Decimal
        # representation if the initial cap has a different exponent.
        if capped.compare_total(step * count) != ZERO:
            high_tangent = None
    except InsufficientDepth:
        return best, 0, False
    # Child intervals share one endpoint and its directional tangent with their
    # parent. Carry that exact tangent instead of walking the same depth again.
    queue = [(-initial_bound, low_count, count, low_tangent, high_tangent)]
    checked = 0
    # Limits CPU independently of the number of decimal ticks or budget magnitude.
    while queue and checked < max_search_evaluations:
        negative_bound, low, high, low_tangent, high_tangent = heapq.heappop(queue)
        if -negative_bound <= best.net_profit_cad:
            continue
        center = (low + high) // 2
        quantity = step * center
        try:
            candidate = evaluate_route(buy, sell, quantity, now=now, costs=costs)
            checked += 1
            if (
                candidate.net_profit_cad is not None
                and candidate.acquisition_cost_cad <= budget
                and (candidate.net_profit_cad, -candidate.acquisition_cost_cad)
                > (best.net_profit_cad, -best.acquisition_cost_cad)
            ):
                best = candidate
        except (ValueError, InsufficientDepth):
            checked += 1
        for first, last in ((low, center - 1), (center + 1, high)):
            if first > last:
                continue
            first_quantity, last_quantity = step * first, step * last
            try:
                first_tangent = (
                    low_tangent
                    if first == low
                    else _continuous_net_slope(buy, sell, first_quantity, costs, left=False)
                )
                last_tangent = (
                    high_tangent
                    if last == high and high_tangent is not None
                    else _continuous_net_slope(buy, sell, last_quantity, costs, left=True)
                )
                bound = _interval_bound(first_quantity, last_quantity, first_tangent, last_tangent)
            except InsufficientDepth:
                return best, checked, False
            if bound > best.net_profit_cad:
                heapq.heappush(queue, (-bound, first, last, first_tangent, last_tangent))
    return best, checked, not queue


@precision
def optimize_route(
    buy: MarketInput,
    sell: MarketInput,
    *,
    budget_cad: Decimal,
    now: datetime,
    costs: Costs = DEFAULT_COSTS,
    budgets: tuple[Decimal, ...] = DEFAULT_BUDGETS,
    max_search_evaluations: int = 4096,
) -> OptimizationResult:
    """Maximize net projected profit over depth boundaries and feasible order steps.

    Capital means fee-inclusive BUY acquisition cost; sell inventory is reported
    separately, never treated as free capital. Results may be losses. No extrapolation.
    """
    exact_decimal(budget_cad)
    if budget_cad <= 0:
        raise ValueError("Budget must be positive")
    if (
        isinstance(max_search_evaluations, bool)
        or not isinstance(max_search_evaluations, int)
        or not 1 <= max_search_evaluations <= 4096
    ):
        raise ValueError("Search evaluation limit must be an integer between 1 and 4096")
    utc_timestamp(now)
    maximum = maximum_quantity(buy, sell)
    evaluated_count = 0
    try:
        maximum_evaluation = evaluate_route(buy, sell, maximum, now=now, costs=costs)
    except (ValueError, InsufficientDepth):
        maximum_evaluation = None
    results: list[BudgetResult] = []
    search_complete = True
    unique_budgets = tuple(dict.fromkeys((budget_cad, *budgets)))
    for budget in unique_budgets:
        exact_decimal(budget)
        if budget <= 0:
            raise ValueError("Budget must be positive")
        if buy.fx is None or buy.fx.fresh_until < now:
            results.append(BudgetResult(budget, None, "CAD_ACQUISITION_CONVERSION_UNAVAILABLE"))
            continue
        try:
            capacity = conversion_capacity(buy.fx.from_cad)
            available_quote = convert(
                buy.fx.from_cad, min(budget, capacity) if capacity is not None else budget
            )
        except InsufficientDepth:
            # Conversion capacity may cap the budget; do not silently use a rate.
            results.append(BudgetResult(budget, None, "INSUFFICIENT_EXECUTABLE_FX_DEPTH"))
            continue
        if buy.fee.currency == "quote":
            available_quote /= ONE + buy.fee.rate
        capped = min(
            maximum, _floor(quantity_for_notional(buy.asks, available_quote), buy.rules.amount_step)
        )
        if sell.fx is not None and sell.fx.fresh_until >= now:
            sale_quote_capacity = conversion_capacity(sell.fx.to_cad)
            if sale_quote_capacity is not None:
                if sell.fee.currency == "quote":
                    sale_quote_capacity /= ONE - sell.fee.rate
                sale_quantity_capacity = quantity_for_notional(sell.bids, sale_quote_capacity)
                capped = min(
                    capped,
                    _floor(
                        sale_quantity_capacity * _base_sell_factor(sell) / _base_buy_factor(buy),
                        buy.rules.amount_step,
                    ),
                )
        candidates = _loss_candidates(buy, sell, capped, costs)
        if candidates is None:
            candidates = _candidate_quantities(buy, sell, capped)
        best: Evaluation | None = None
        for quantity in sorted(candidates):
            try:
                result = evaluate_route(buy, sell, quantity, now=now, costs=costs)
                evaluated_count += 1
            except (ValueError, InsufficientDepth):
                continue
            if result.acquisition_cost_cad is None or result.acquisition_cost_cad > budget:
                continue
            if result.net_profit_cad is None:
                continue
            if best is None or (result.net_profit_cad, -result.acquisition_cost_cad) > (
                best.net_profit_cad,
                -best.acquisition_cost_cad,
            ):
                best = result
        best, additional, certified = _certify_optimum(
            buy,
            sell,
            capped,
            budget,
            now,
            costs,
            best,
            max_search_evaluations,
        )
        evaluated_count += additional
        search_complete &= certified
        if best is not None and not certified:
            best = replace(
                best,
                verified=False,
                reasons=(*best.reasons, "OPTIMIZER_GLOBAL_OPTIMUM_UNCONFIRMED"),
            )
        results.append(
            BudgetResult(budget, best, None if best else "NO_FEASIBLE_SIZE_WITH_CAD_EVIDENCE")
        )
    primary = next(result for result in results if result.budget_cad == budget_cad)
    reasons = () if primary.best else (primary.reason,)
    if not search_complete:
        reasons += ("OPTIMIZER_GLOBAL_OPTIMUM_UNCONFIRMED",)
    return OptimizationResult(
        primary.best,
        maximum_evaluation,
        tuple(results),
        evaluated_count,
        reasons,
        "exact depth/lattice optimum certified by concave bounds"
        if search_complete
        else "best evaluated feasible size; global optimum unconfirmed within CPU budget",
    )


