"""Application-scoped, externally checked UTC; never adjusts the operating system clock.

HTTPS time endpoints are independent corroborating sources, not atomic clocks. Their
precision, network uncertainty and agreement are explicit. An untrusted reading is
diagnostic only and must not establish financial freshness.
"""

import asyncio
import itertools
import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp


def elapsed_seconds() -> float:
    """Unadjusted monotonic time avoids NTP frequency slewing on Linux/WSL."""
    if hasattr(time, "CLOCK_MONOTONIC_RAW"):
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    return time.monotonic()


ELAPSED_SOURCE = "CLOCK_MONOTONIC_RAW" if hasattr(time, "CLOCK_MONOTONIC_RAW") else "monotonic"


