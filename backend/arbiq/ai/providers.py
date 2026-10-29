"""Optional NVIDIA prioritization of deterministic explanations. No financial authority.

The model may only reorder server-authored explanation IDs. Its free text, numbers,
tool calls and instructions are never rendered or executed.
"""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from datetime import datetime
from typing import Literal, Protocol

import aiohttp
from pydantic import Field

from arbiq.ai.contracts import NumericReport
from arbiq.ai.explanations import deterministic_notes
from arbiq.finance.contracts import Contract

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
MAX_RESPONSE_BYTES = 32768


class Explanation(Contract):
    status: Literal["disabled", "unavailable", "complete"]
    text: str | None = None
    provider: str
    reason: str | None = None
    cached: bool = False
    deterministic: bool = True
    numeric_inputs: dict[str, str | None] = Field(default_factory=dict)


def validated_report(value):
    return value if isinstance(value, NumericReport) else NumericReport.model_validate(value)


def fallback(report, *, status="disabled", reason="AI_DISABLED", provider="none"):
    report = validated_report(report)
    return Explanation(
        status=status,
        provider=provider,
        reason=reason,
        text="\n\n".join(deterministic_notes(report).values()),
        numeric_inputs=report.numeric_inputs,
    )


class AnalysisProvider(Protocol):
    """Explanations only. No tools, quantitative decisions or trade execution."""

    async def explain(self, report, *, now: datetime | None = None) -> Explanation: ...


class DisabledProvider:
    async def explain(self, report, *, now=None) -> Explanation:
        return fallback(report)


class NvidiaProvider:
    def __init__(self, settings=None, *, transport=None, monotonic=time.monotonic):
        self.settings = settings
        self.transport = transport or self._request
        self.monotonic = monotonic
        self.lock = asyncio.Lock()
        self.cache = OrderedDict()
        self.last_attempt = None
        self.daily_attempts = 0
        self.attempt_date = None

    def disabled_reason(self, now):
        s = self.settings
        if s is None or not getattr(s, "ai_enabled", False):
            return "AI_DISABLED"
        if not s.nvidia_api_key.get_secret_value() or not s.nvidia_model:
            return "NVIDIA_CONFIGURATION_MISSING"
        expiry = getattr(s, "ai_free_allowance_expires_at", None)
        if not getattr(s, "ai_free_allowance_authorized", False) or expiry is None:
            return "FREE_ALLOWANCE_NOT_AUTHORIZED"
        if now is None or now.tzinfo is None:
            return "TRUSTED_UTC_REQUIRED"
        if now >= expiry:
            return "FREE_ALLOWANCE_AUTHORIZATION_EXPIRED"
        return None

