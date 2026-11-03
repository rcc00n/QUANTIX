#!/usr/bin/env python3
"""Derive a conservative capacity envelope from genuine bounded local profiles.

The sanitized proof is evidence for a fresh read-only VPS preflight, never proof
of production coexistence or cold-cache behavior. No network or Docker call occurs.
"""

import argparse
import base64
import hashlib
import itertools
import json
import os
import re
from decimal import Decimal, localcontext
from pathlib import Path

if __package__:
    from .production_preflight import (
        MIN_AFTER_SECONDS,
        MIN_PEAK_SAVING_MIB,
        REQUIRED_READ_CATEGORIES,
        REQUIRED_ROLES,
        RESERVE_MIB,
        SCHEMA,
        STARTUP_MARGIN_MIB,
        CapacityProofError,
        bounded_json,
        canonical,
        decimal_value,
        exact_int,
        planning_envelope,
        seal_proof,
        validate_capacity_proof,
    )
else:
    from production_preflight import (
        MIN_AFTER_SECONDS,
        MIN_PEAK_SAVING_MIB,
        REQUIRED_READ_CATEGORIES,
        REQUIRED_ROLES,
        RESERVE_MIB,
        SCHEMA,
        STARTUP_MARGIN_MIB,
        CapacityProofError,
        bounded_json,
        canonical,
        decimal_value,
        exact_int,
        planning_envelope,
        seal_proof,
        validate_capacity_proof,
    )

ROOT = Path(__file__).resolve().parents[1]
MAX_PROFILE_BYTES = 4 * 1024 * 1024
PROFILE_KIND = "ACTUAL_LOCAL_NORMAL_READS_AND_RETAINED_EXPORT"
UNITS = {
    "B": Decimal(1),
    "KiB": Decimal(1024),
    "MiB": Decimal(1024) ** 2,
    "GiB": Decimal(1024) ** 3,
    "TiB": Decimal(1024) ** 4,
    "kB": Decimal(1000),
    "KB": Decimal(1000),
    "MB": Decimal(1000) ** 2,
    "GB": Decimal(1000) ** 3,
    "TB": Decimal(1000) ** 4,
}


def docker_memory_mib(value):
    if not isinstance(value, str) or len(value) > 100:
        raise CapacityProofError("INVALID_DOCKER_MEMORY")
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|kB|KB|MB|GB|TB)",
        value.split("/", 1)[0].strip(),
    )
    if match is None:
        raise CapacityProofError("INVALID_DOCKER_MEMORY")
    with localcontext() as context:
        context.prec = 80
        number = Decimal(match[1]) * UNITS[match[2]] / (Decimal(1024) ** 2)
    if not 0 < number <= Decimal(1000000):
        raise CapacityProofError("INVALID_DOCKER_MEMORY")
    return number


def read_category(path):
    if not isinstance(path, str) or len(path) > 300:
        raise CapacityProofError("INVALID_READ_RECORD")
    if path == "terminal/overview":
        return "overview"
    if path == "opportunities?history=true&limit=50":
        return "opportunities_default"
    if path == "opportunities?history=true&limit=25&budget_cad=250":
        return "opportunities_custom"
    if re.fullmatch(r"analytics/sessions/[a-f0-9-]{36}", path):
        return "analytics"
    raise CapacityProofError("UNRECOGNIZED_READ_WORKLOAD")


