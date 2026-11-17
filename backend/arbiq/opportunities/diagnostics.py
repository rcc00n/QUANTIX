"""Bounded, historical size diagnostics from pinned depth. No writes or forecasts."""

from dataclasses import asdict
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from arbiq.market_data.quality import safe_json
from arbiq.opportunities.analysis import load_costs, load_input
from arbiq.opportunities.contracts import COMPARISON_CAPITALS
from arbiq.opportunities.math import (
    InsufficientDepth,
    conversion_capacity,
    convert,
    evaluate_route,
    maximum_quantity,
    optimize_route,
    precision,
    quantity_for_notional,
)
from arbiq.paper_trading.execution import planned_reserves

ONE = Decimal(1)


def projection(value):
    if value is None:
        return None
    return safe_json(asdict(value))


def quantity_caps(buy, sell):
    """Express each observed market cap in the requested buy base quantity."""
    received_base_factor = ONE - buy.fee.rate if buy.fee.currency == "base" else ONE
    sold_base_factor = ONE + sell.fee.rate if sell.fee.currency == "base" else ONE
    result = {}
    for side, market, levels, factor in (
        ("BUY", buy, buy.asks, ONE),
        ("SELL", sell, sell.bids, sold_base_factor / received_base_factor),
    ):
        result[f"{side}_OBSERVED_DEPTH"] = sum(level.quantity for level in levels) * factor
        if market.rules.max_quantity is not None:
            result[f"{side}_MAXIMUM_QUANTITY"] = market.rules.max_quantity * factor
        if market.rules.max_notional is not None and market.rules.max_notional <= sum(
            level.price * level.quantity for level in levels
        ):
            result[f"{side}_MAXIMUM_NOTIONAL"] = (
                quantity_for_notional(levels, market.rules.max_notional) * factor
            )
    return result


def exceeded_caps(quantity, caps):
    return [reason for reason, limit in caps.items() if quantity > limit]


def display_size(value, buy, sell, costs):
    if value is None:
        return {"estimated_costs": None, "planned_buy_reserve": None, "planned_sell_reserve": None}
    try:
        buy_reserve, sell_reserve = planned_reserves(value, buy.fee, sell.fee, costs.slippage_bps)
    except (ValueError, ArithmeticError):
        buy_reserve = sell_reserve = None
    return safe_json(
        {
            "estimated_costs": {
                "fees_cad": value.fees_cad,
                "fx_drag_cad": value.fx_cost_cad,
                "slippage_cad": value.slippage_reserve_cad,
                "rebalancing_cad": value.rebalancing_cost_cad,
            },
            "planned_buy_reserve": buy_reserve,
            "planned_sell_reserve": sell_reserve,
        }
    )


def best_boundary(best, buy, sell, budget, now, costs, caps):
    if best.acquisition_cost_cad == budget:
        return ["BUY_CAPITAL_BUDGET"]
    if buy.rules.amount_step is None:
        return ["BUY_QUANTITY_STEP_UNVERIFIED"]
    if limited := exceeded_caps(best.buy_quantity + buy.rules.amount_step, caps):
        return limited
    try:
        next_size = evaluate_route(
            buy, sell, best.buy_quantity + buy.rules.amount_step, now=now, costs=costs
        )
    except (ValueError, ArithmeticError) as exc:
        code = str(exc)
        return [code if code.isupper() else "NEXT_QUANTITY_UNAVAILABLE"]
    if next_size.acquisition_cost_cad is None:
        return [code for code in next_size.reasons if "CONVERSION" in code or "FX_DEPTH" in code]
    if next_size.acquisition_cost_cad > budget:
        return ["BUY_CAPITAL_BUDGET_AT_NEXT_QUANTITY_STEP"]
    if next_size.net_profit_cad <= best.net_profit_cad:
        return ["NEXT_QUANTITY_STEP_DOES_NOT_IMPROVE_NET_PROFIT"]
    return ["NO_ACTIVE_CONSTRAINT_CONFIRMED"]


@precision
def size_diagnostics(
    evidence: dict,
    selected_budget: Decimal = Decimal(1000),
    *,
    max_search_evaluations: int = 4096,
) -> dict:
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
    max_search_evaluations = min(
        max_search_evaluations,
        inputs.get("max_search_evaluations", max_search_evaluations),
    )
    result = optimize_route(
        buy,
        sell,
        budget_cad=selected_budget,
        now=now,
        costs=costs,
        budgets=(),
        max_search_evaluations=max_search_evaluations,
    )
    blocked = tuple(
        reason
        for reason in result.reasons
        if reason.endswith(("MARKET_ORDER_UNAVAILABLE", "MARKET_RULES_NOT_YET_AVAILABLE"))
    )
    maximum = None if blocked else maximum_quantity(buy, sell)
    caps = {} if blocked else quantity_caps(buy, sell)
    observed_buy_notional = sum(level.price * level.quantity for level in buy.asks)
    rows = []
    for target in COMPARISON_CAPITALS:
        reasons = []
        limiting = []
        value = None
        try:
            if blocked:
                reasons.extend(blocked)
                limiting.extend(blocked)
                raise ValueError(blocked[0])
            if buy.fx is None:
                raise ValueError("BUY_CAD_CONVERSION_UNAVAILABLE")
            if buy.fx.fresh_until < now:
                raise ValueError("BUY_CAD_CONVERSION_STALE")
            if any(at is not None and at > now for at in (buy.fx.available_at, buy.fx.observed_at)):
                raise ValueError("BUY_CAD_CONVERSION_NOT_YET_AVAILABLE")
            capacity = conversion_capacity(buy.fx.from_cad)
            if capacity is not None and target > capacity:
                raise ValueError("BUY_EXECUTABLE_FX_DEPTH")
            quote = convert(buy.fx.from_cad, target)
            if buy.fee.currency == "quote":
                quote /= 1 + buy.fee.rate
            quantity = quantity_for_notional(buy.asks, quote)
            # quantity_for_notional deliberately saturates at visible depth. A
            # saturated result does not establish support for the requested size.
            if observed_buy_notional < quote:
                limiting.append("BUY_OBSERVED_DEPTH")
                raise ValueError("TARGET_EXCEEDS_BUY_OBSERVED_DEPTH")
            step = buy.rules.amount_step or Decimal("1e-18")
            quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if quantity > maximum:
                limiting.extend(exceeded_caps(quantity, caps))
                raise ValueError("TARGET_EXCEEDS_COMBINED_DEPTH_OR_MARKET_MAXIMUM")
            value = evaluate_route(buy, sell, quantity, now=now, costs=costs)
            if value.net_profit_cad is None:
                reasons.extend(value.reasons)
                limiting.extend(
                    reason
                    for reason in value.reasons
                    if reason == "INSUFFICIENT_EXECUTABLE_FX_DEPTH"
                    or "_CAD_CONVERSION_" in reason
                    and not reason.endswith("_UNVERIFIED")
                )
                value = None
            else:
                limiting.extend(
                    exceeded_caps(quantity + step, caps)
                    or [
                        "BUY_CAPITAL_TARGET_AT_QUANTITY_STEP"
                        if value.acquisition_cost_cad < target
                        else "BUY_CAPITAL_TARGET"
                    ]
                )
        except (ValueError, ArithmeticError) as exc:
            reason = str(exc)
            if isinstance(exc, InsufficientDepth):
                reason = "TARGET_EXCEEDS_EXECUTABLE_FX_OR_BOOK_DEPTH"
            # Financial errors are stable codes, never arbitrary provider text.
            reasons.append(reason if reason.isupper() else "TARGET_INPUTS_UNAVAILABLE")
            if not limiting:
                limiting.extend(reasons)
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
                "limiting_constraints": list(dict.fromkeys(limiting)),
                **display_size(value, buy, sell, costs),
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
        "best_costs_and_reserves": display_size(best, buy, sell, costs),
        "best_limiting_constraints": (
            best_boundary(best, buy, sell, selected_budget, now, costs, caps)
            if best is not None
            else list(result.reasons)
        ),
        "comparisons": rows,
        "explanations": list(dict.fromkeys(explanations)),
        "data_quality": quality,
        "optimizer": {
            "evaluated_count": result.evaluated_count,
            "optimality": result.optimality,
            "max_search_evaluations": max_search_evaluations,
            "certified": (
                best is not None and "OPTIMIZER_GLOBAL_OPTIMUM_UNCONFIRMED" not in result.reasons
            ),
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
