"""Bounded, historical size diagnostics from pinned depth. No writes or forecasts."""

from dataclasses import asdict
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from arbiq.market_data.quality import safe_json
from arbiq.opportunities.analysis import load_costs, load_input
from arbiq.opportunities.math import (
    InsufficientDepth,
    convert,
    evaluate_route,
    maximum_quantity,
    optimize_route,
    precision,
    quantity_for_notional,
)

COMPARISON_CAPITALS = tuple(Decimal(x) for x in (50, 100, 250, 500, 1000))


def projection(value):
    if value is None:
        return None
    return safe_json(asdict(value))


@precision
def size_diagnostics(evidence: dict, selected_budget: Decimal = Decimal(1000)) -> dict:
    """Compare actual rounded target sizes, rather than repeating a tiny optimum.

    A budget optimum is not a claim that the entire budget has profitable depth.
    Unsupported target rows carry no profit. These are projections at the original
    decision time, never fresh quotes or guaranteed fills.
    """
    inputs = evidence.get("calculation_inputs")
    if not isinstance(inputs, dict):
        raise ValueError("PINNED_CALCULATION_INPUTS_UNAVAILABLE")
    buy, sell = load_input(inputs["buy"]), load_input(inputs["sell"])
    costs, now = load_costs(inputs["costs"]), datetime.fromisoformat(inputs["evaluated_at"])
    result = optimize_route(
        buy,
        sell,
        budget_cad=selected_budget,
        now=now,
        costs=costs,
        budgets=(),
        max_search_evaluations=4096,
    )
    maximum = maximum_quantity(buy, sell)
    rows = []
    for target in COMPARISON_CAPITALS:
        reasons = []
        value = None
        quantity = None
        try:
            if buy.fx is None:
                raise ValueError("BUY_CAD_CONVERSION_UNAVAILABLE")
            quote = convert(buy.fx.from_cad, target)
            if buy.fee.currency == "quote":
                quote /= 1 + buy.fee.rate
            quantity = quantity_for_notional(buy.asks, quote)
            # quantity_for_notional deliberately saturates at visible depth. A
            # saturated result does not establish support for the requested size.
            if sum((level.price * level.quantity for level in buy.asks), Decimal(0)) < quote:
                raise ValueError("TARGET_EXCEEDS_BUY_OBSERVED_DEPTH")
            step = buy.rules.amount_step or Decimal("1e-18")
            quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if quantity > maximum:
                raise ValueError("TARGET_EXCEEDS_COMBINED_DEPTH_OR_MARKET_MAXIMUM")
            value = evaluate_route(buy, sell, quantity, now=now, costs=costs)
            if value.net_profit_cad is None:
                reasons.extend(value.reasons)
                value = None
        except (ValueError, ArithmeticError) as exc:
            reason = str(exc)
            if isinstance(exc, InsufficientDepth):
                reason = "TARGET_EXCEEDS_EXECUTABLE_FX_OR_BOOK_DEPTH"
            # Financial errors are stable codes, never arbitrary provider text.
            reasons.append(reason if reason.isupper() else "TARGET_INPUTS_UNAVAILABLE")
        rows.append(
            {
                "requested_buy_capital_cad": str(target),
                "supported": value is not None,
                "buy_quantity": str(value.buy_quantity) if value else None,
                "buy_capital_cad": str(value.acquisition_cost_cad) if value else None,
                "estimated_net_profit_cad": str(value.net_profit_cad) if value else None,
                "estimated_net_return_pct": str(value.net_return_pct) if value else None,
                "required_sell_inventory": str(value.sell_base_required) if value else None,
                "reasons": list(dict.fromkeys(reasons)),
                "projection": projection(value),
            }
        )
    best = result.best
    explanations = []
    if best is None:
        explanations.extend(result.reasons)
    else:
        explanations.append("OBJECTIVE_IS_MAXIMUM_NET_CAD_PROFIT_NOT_MAXIMUM_SPEND")
        if best.acquisition_cost_cad < selected_budget:
            explanations.append("SELECTED_OPTIMUM_USES_LESS_THAN_MAXIMUM_BUY_BUDGET")
        if any(
            x["supported"] and Decimal(x["estimated_net_profit_cad"]) < best.net_profit_cad
            for x in rows
        ):
            explanations.append("LARGER_SUPPORTED_SIZES_HAVE_LOWER_NET_CAD_PROFIT")
        if any(not x["supported"] for x in rows):
            explanations.append("SOME_TARGET_SIZES_LACK_EXECUTABLE_DEPTH_OR_VALID_CONVERSION")
        if best.residual_base:
            explanations.append("STEP_SIZES_OR_BASE_FEES_LEAVE_UNMATCHED_BASE_DUST")
        if best.sell_quantity == sell.bids[0].quantity and len(sell.bids) > 1:
            explanations.append("BEST_SIZE_AT_SELL_TOP_LEVEL_DEPTH_BOUNDARY")
        explanations.extend(best.reasons)
    quality = {side: evidence.get(f"{side}_quality", "UNAVAILABLE") for side in ("buy", "sell")}
    if "SUSPICIOUS" in quality.values():
        explanations.append("SUSPICIOUS_RETAINED_BOOK_NOT_VERIFIED")
    return {
        "schema_version": "quantix-size-diagnostics-v1",
        "attribution": "retained evidence at original evaluation time; not a fresh quote",
        "evaluated_at": now.isoformat(),
        "selected_maximum_buy_budget_cad": str(selected_budget),
        "objective": "maximum projected net CAD profit within the buy budget",
        "best": projection(best),
        "comparisons": rows,
        "explanations": list(dict.fromkeys(explanations)),
        "data_quality": quality,
        "optimizer": {
            "evaluated_count": result.evaluated_count,
            "optimality": result.optimality,
            "max_search_evaluations": 4096,
        },
        "market_constraints": safe_json(
            {
                "buy": asdict(buy.rules),
                "sell": asdict(sell.rules),
                "maximum_combined_buy_quantity": maximum,
                "buy_fee": asdict(buy.fee),
                "sell_fee": asdict(sell.fee),
                "buy_quote_currency": buy.quote_currency,
                "sell_quote_currency": sell.quote_currency,
                "sell_inventory_currency": sell.base_currency,
                "buy_ask_levels": len(buy.asks),
                "sell_bid_levels": len(sell.bids),
            }
        ),
        "actionable": False,
        "actual_execution": False,
    }
