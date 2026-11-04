#!/usr/bin/env python3
# ruff: noqa: UP017 -- The audited Ubuntu 22.04 VPS uses Python 3.10.
"""Read-only VPS gate for cold deployment or one already-stopped image replacement.

Run over pinned SSH stdin. Optional cold-deployment proofs contain only validated
resource metrics and artifact hashes. The replacement gate measures actual Linux
availability after stop and returns a result for one service.
"""

import argparse
import base64
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext

DEFAULT_PLANNING_MIB = 1536
RESERVE_MIB = 512
STARTUP_MARGIN_MIB = 64
MIN_AFTER_SECONDS = 900
MIN_PEAK_SAVING_MIB = 200
BUDGET_QUANTUM_MIB = 64
PEAK_FIXED_MARGIN_MIB = 128
PEAK_FACTOR = Decimal("1.10")
MAX_PROOF_BYTES = 16384
MAX_PROOF_BASE64_BYTES = 4 * ((MAX_PROOF_BYTES + 2) // 3)
SCHEMA = "quantix-capacity-proof-v1"
REQUIRED_ROLES = ("backend", "collector", "db", "frontend", "notifications")
MIB = 1024**2
PRODUCTION_MEMORY_MIB = {
    "backend": 384,
    "collector": 768,
    "db": 320,
    "frontend": 192,
    "notifications": 160,
}
REPLACEABLE_ROLES = ("backend", "collector", "frontend", "notifications")
REPLACEMENT_ROLES = {
    "stage8a1": REPLACEABLE_ROLES,
    "stage8b1": ("backend", "collector", "frontend"),
    "stage8c": ("backend", "collector", "frontend"),
}
GUARD_SUSTAINED_FLOOR_MIB = 640
REPLACEMENT_DISK_RESERVE_BYTES = 6 * 1024**3
PRODUCTION_ROOT = pathlib.Path("/opt/quantix")
LOCAL_DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
REQUIRED_READ_CATEGORIES = (
    "analytics",
    "opportunities_custom",
    "opportunities_default",
    "overview",
)


class CapacityProofError(ValueError):
    """Safe code-only error; never echo raw proof or profile contents."""


def decimal_value(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise CapacityProofError("INVALID_NUMERIC_METRIC")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise CapacityProofError("INVALID_NUMERIC_METRIC") from None
    if (
        not number.is_finite()
        or number < 0
        or number > Decimal(1000000)
        or len(str(value)) > 128
        or number.as_tuple().exponent < -32
    ):
        raise CapacityProofError("INVALID_NUMERIC_METRIC")
    return number


def exact_int(value, *, minimum=0, maximum=100000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CapacityProofError("INVALID_INTEGER_METRIC")
    return value


