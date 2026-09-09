"""Strict numeric explanation inputs. Free text and secrets never enter a model prompt."""

import re
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from arbiq.finance.contracts import Contract

METRICS = frozenset(
    {
        "buy_capital_cad",
        "net_profit_cad",
        "net_return_pct",
        "sell_base_required",
        "selected_budget_cad",
        "fees_cad",
        "expenses_cad",
        "residual_base",
        "completed",
        "partial",
        "failed",
        "missed",
        "expired",
        "detections",
        "net_completed_trade_pnl_cad",
        "realized_simulated_pnl_cad",
        "unrealized_pnl_cad",
        "equity_cad",
        "no_trading_benchmark_cad",
        "strategy_excess_cad",
    }
)


class NumericReport(Contract):
    schema_version: Literal["quantix-explanation-input-v1"] = "quantix-explanation-input-v1"
    source_type: Literal["opportunity", "paper_report"] = "opportunity"
    classification: Literal["RESEARCH", "VALIDATED", "UNVERIFIED"] = "UNVERIFIED"
    numeric_inputs: dict[str, str | None] = Field(default_factory=dict, max_length=32)
    # Only known deterministic reason codes are retained by the API builder.
    reasons: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("numeric_inputs")
    @classmethod
    def numeric_only(cls, values):
        if set(values) - METRICS:
            raise ValueError("Unknown explanation metric")
        for value in values.values():
            if value is None:
                continue
            if len(value) > 110 or not re.fullmatch(r"-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?", value):
                raise ValueError("Exact finite decimal strings required")
            if not Decimal(value).is_finite() or Decimal(value).copy_abs() >= Decimal("1e70"):
                raise ValueError("Numeric explanation input out of bounds")
        return values

    @field_validator("reasons")
    @classmethod
    def reason_codes(cls, values):
        from arbiq.ai.explanations import REASON_TEXT

        if set(values) - REASON_TEXT.keys():
            raise ValueError("Unknown explanation reason code")
        return values


