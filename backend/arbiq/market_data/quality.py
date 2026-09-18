"""Deterministic book validation. Preserve anomalies; never infer opportunities."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from arbiq.collector.config import CollectorConfig
from arbiq.finance.contracts import Contract, UTCTimestamp, exact_decimal
from arbiq.market_data.contracts import BookLevel, NormalizedOrderBook


class DataState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    SUSPICIOUS = "SUSPICIOUS"
    UNAVAILABLE = "UNAVAILABLE"


