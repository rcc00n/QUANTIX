"""Serialization and deterministic recomputation from pinned inputs (no future data)."""

from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from arbiq.market_data.contracts import BookLevel
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.contracts import (
    ConversionLeg,
    ConversionLevel,
    ConversionRoute,
    Costs,
    Fee,
    FXConversion,
    MarketInput,
    MarketRules,
)
from arbiq.opportunities.math import optimize_route


def dump_input(value):
    """Retain full Pydantic levels nested inside frozen financial dataclasses.

    dataclasses.asdict does not serialize Pydantic models; passing such a model
    directly to the generic public scalar serializer would lose its structure.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: dump_input(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): dump_input(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [dump_input(item) for item in value]
    return safe_json(value)


def load_input(data):
    fx = data.get("fx")

    def route(value):
        return ConversionRoute(
            tuple(
                ConversionLeg(
                    x["source_currency"],
                    x["target_currency"],
                    tuple(
                        ConversionLevel(Decimal(z["price"]), Decimal(z["quantity"]))
                        for z in x["levels"]
                    ),
                    Decimal(x["fee_rate"]),
                    x["evidence"],
                )
                for x in value["legs"]
            )
        )

    if fx:
        fx = FXConversion(
            fx["currency"],
            route(fx["to_cad"]),
            route(fx["from_cad"]),
            fx["verified"],
            datetime.fromisoformat(fx["fresh_until"]),
            fx["source"],
            observed_at=datetime.fromisoformat(fx["observed_at"])
            if fx.get("observed_at")
            else None,
            available_at=datetime.fromisoformat(fx["available_at"])
            if fx.get("available_at")
            else None,
        )
    return MarketInput(
        data["market_id"],
        data["venue"],
        data["base_currency"],
        data["quote_currency"],
        tuple(BookLevel(**x) for x in data["asks"]),
        tuple(BookLevel(**x) for x in data["bids"]),
        Fee(**{**data["fee"], "rate": Decimal(data["fee"]["rate"])}),
        MarketRules(
            **{
                k: (Decimal(v) if v is not None and k not in {"verified", "source"} else v)
                for k, v in data["rules"].items()
            }
        ),
        fx,
    )


def load_costs(data):
    return Costs(
        **{
            key: Decimal(value)
            if key in {"slippage_bps", "rebalancing_bps", "fixed_rebalancing_cad"}
            else value
            for key, value in data.items()
        }
    )


def resize(payload, budget, *, max_search_evaluations=4096):
    """Re-evaluate at the original decision time, explicitly historical attribution."""
    evidence = payload.get("evidence", {})
    inputs = evidence.get("calculation_inputs")
    if not inputs:
        return {
            **payload,
            "quantity": None,
            "net_profit_cad": None,
            "net_return_pct": None,
            "acquisition_cost_cad": None,
            "reasons": [*payload["reasons"], "BUDGET_EVIDENCE_UNAVAILABLE"],
        }
    costs = load_costs(inputs["costs"])
    result = optimize_route(
        load_input(inputs["buy"]),
        load_input(inputs["sell"]),
        budget_cad=budget,
        now=datetime.fromisoformat(inputs["evaluated_at"]),
        costs=costs,
        budgets=(),
        max_search_evaluations=max_search_evaluations,
    )
    best = result.best
    reasons = list(payload.get("reasons", []))
    reasons.extend(best.reasons if best else result.reasons)
    budget_feasibility = "FEASIBLE_PROJECTED_SIZE"
    if best is None:
        reasons.append("SELECTED_BUDGET_HAS_NO_FEASIBLE_SIZE")
        budget_feasibility = "NO_FEASIBLE_SIZE"
    elif best.net_profit_cad is not None and best.net_profit_cad <= 0:
        reasons.append("SELECTED_BUDGET_NON_POSITIVE_NET_AFTER_COSTS")
        budget_feasibility = "NON_POSITIVE_NET"
    return {
        **payload,
        "selected_budget_cad": str(budget),
        "quantity": str(best.buy_quantity) if best else None,
        "buy_price": str(best.buy_vwap) if best else None,
        "sell_price": str(best.sell_vwap) if best else None,
        "net_profit_cad": str(best.net_profit_cad)
        if best and best.net_profit_cad is not None
        else None,
        "net_return_pct": str(best.net_return_pct)
        if best and best.net_return_pct is not None
        else None,
        "acquisition_cost_cad": str(best.acquisition_cost_cad)
        if best and best.acquisition_cost_cad is not None
        else None,
        "costs": safe_json(asdict(best)) if best else {},
        "reasons": list(dict.fromkeys(reasons)),
        "actionable": False,
        "budget_feasibility": budget_feasibility,
        "budget_results": safe_json([asdict(value) for value in result.budgets]),
        "optimizer": {
            "evaluated_count": result.evaluated_count,
            "optimality": result.optimality,
            "max_search_evaluations": max_search_evaluations,
        },
        "budget_recalculation": (
            "retained evidence at original evaluation time; not a fresh quote; "
            f"{max_search_evaluations}-evaluation search bound; optimality is reported explicitly"
        ),
    }
