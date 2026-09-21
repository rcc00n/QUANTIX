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


