"""Optional analysis uses SYNTHETIC reports and mock transports; no external requests."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from arbiq.ai.api import explain, router
from arbiq.ai.contracts import ExplainRequest, NumericReport
from arbiq.ai.explanations import build_report, display_decimal
from arbiq.ai.providers import DisabledProvider, NvidiaProvider
from arbiq.auth.dependencies import current_user
from arbiq.db.session import get_db
from fastapi import FastAPI, Request
from pydantic import SecretStr, ValidationError

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def settings(**changes):
    return SimpleNamespace(
        **{
            "ai_enabled": True,
            "nvidia_api_key": SecretStr("SYNTHETIC-secret-not-real"),
            "nvidia_model": "synthetic/model",
            "ai_free_allowance_authorized": True,
            "ai_free_allowance_expires_at": NOW + timedelta(days=1),
            "ai_max_requests_per_day": 2,
            "ai_min_request_interval_seconds": 60,
            "ai_cache_ttl_seconds": 300,
            "ai_request_timeout_seconds": 1,
            "ai_max_input_bytes": 12000,
            "ai_max_output_tokens": 400,
            **changes,
        }
    )


def report(capital="6.71"):
    return NumericReport(
        numeric_inputs={"buy_capital_cad": capital, "net_profit_cad": "0.5"}
    )


def response(content=None, **message):
    return {
        "choices": [
            {
                "message": {
                    "content": content
                    or json.dumps({"reason_order": ["capital", "profit"]}),
                    **message,
                }
            }
        ]
    }


async def test_disabled_provider_still_explains_actual_values():
    value = await DisabledProvider().explain(report())
    assert value.status == "disabled"
    assert "C$6.71" in value.text and "default C$50" in value.text
    assert "not executed profit" in value.text


async def test_human_explanation_trims_storage_zeroes_without_changing_numeric_inputs():
    source = NumericReport(
        source_type="paper_report",
        classification="RESEARCH",
        numeric_inputs={
            "completed": "0",
            "equity_cad": "10000.000000000000000000",
            "realized_simulated_pnl_cad": "0E-18",
            "unrealized_pnl_cad": "-0E-18",
        },
    )
    value = await DisabledProvider().explain(source)
    assert "equity: C$10000." in value.text
    assert "P&L: C$0." in value.text
    assert "0E-18" not in value.text and "10000.000000" not in value.text
    assert value.numeric_inputs == source.numeric_inputs
    assert value.numeric_inputs["realized_simulated_pnl_cad"] == "0E-18"
    assert display_decimal("1.234567890123456789000") == "1.234567890123456789"
    assert display_decimal("1E-999999") == "1E-999999"


























