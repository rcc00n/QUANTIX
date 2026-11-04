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


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise CapacityProofError("INVALID_PROOF_STRUCTURE") from None


def seal_proof(value):
    result = dict(value)
    result.pop("proof_sha256", None)
    result["proof_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def bounded_json(data, maximum):
    if not isinstance(data, bytes) or len(data) > maximum:
        raise CapacityProofError("EVIDENCE_TOO_LARGE")

    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise CapacityProofError("DUPLICATE_JSON_KEY")
            result[key] = item
        return result

    def nonfinite(_value):
        raise CapacityProofError("NONFINITE_JSON_NUMBER")

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=nonfinite,
            object_pairs_hook=unique_pairs,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise CapacityProofError("INVALID_JSON_EVIDENCE") from None
    if not isinstance(value, dict):
        raise CapacityProofError("INVALID_PROOF_STRUCTURE")
    return value


def planning_envelope(after_peak_mib):
    peak = decimal_value(after_peak_mib)
    if peak <= 0:
        raise CapacityProofError("EMPTY_PEAK_MEASUREMENT")
    with localcontext() as context:
        context.prec = 80
        bound = max(peak * PEAK_FACTOR, peak + PEAK_FIXED_MARGIN_MIB)
        return (
            int((bound / BUDGET_QUANTUM_MIB).to_integral_value(rounding=ROUND_CEILING))
            * BUDGET_QUANTUM_MIB
        )


