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


