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


