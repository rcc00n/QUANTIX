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


def validate_summary(value, *, after):
    expected = {
        "elapsed_seconds",
        "sample_count",
        "sample_first_seconds",
        "sample_last_seconds",
        "maximum_sample_gap_seconds",
        "stack_peak_mib",
        "service_peak_mib",
        "read_count",
        "read_status_codes",
        "read_categories",
        "coverage_sample_count",
        "minimum_coverage",
        "export_exit_code",
        "external_exchange_requests_forced",
        "stable_service_instances",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CapacityProofError("INVALID_PROFILE_SUMMARY")
    elapsed = decimal_value(value["elapsed_seconds"])
    first = decimal_value(value["sample_first_seconds"])
    last = decimal_value(value["sample_last_seconds"])
    gap = decimal_value(value["maximum_sample_gap_seconds"])
    exact_int(value["sample_count"], minimum=2, maximum=10000)
    if (
        elapsed <= 0
        or not 0 <= first < last <= elapsed
        or first > 30
        or elapsed - last > 30
        or gap > 30
    ):
        raise CapacityProofError("INCOMPLETE_OBSERVATION_WINDOW")
    if after and elapsed < MIN_AFTER_SECONDS:
        raise CapacityProofError("AFTER_PROFILE_SHORTER_THAN_900_SECONDS")
    if value["stable_service_instances"] is not True:
        raise CapacityProofError("SERVICE_RESTART_DURING_MEASUREMENT")
    peaks = value["service_peak_mib"]
    if not isinstance(peaks, dict) or set(peaks) != set(REQUIRED_ROLES):
        raise CapacityProofError("INCOMPLETE_SERVICE_MEASUREMENT")
    numbers = [decimal_value(peaks[role]) for role in REQUIRED_ROLES]
    peak = decimal_value(value["stack_peak_mib"])
    with localcontext() as context:
        context.prec = 80
        separate_peak_sum = sum(numbers, Decimal(0))
    if any(number <= 0 for number in numbers) or not max(numbers) <= peak <= separate_peak_sum:
        raise CapacityProofError("INCONSISTENT_PEAK_MEASUREMENT")
    exact_int(value["read_count"], minimum=len(REQUIRED_READ_CATEGORIES), maximum=10000)
    if value["read_status_codes"] != [200] or any(
        type(x) is not int for x in value["read_status_codes"]
    ):
        raise CapacityProofError("NORMAL_READ_FAILED")
    categories = value["read_categories"]
    if not isinstance(categories, list) or set(categories) != set(REQUIRED_READ_CATEGORIES):
        raise CapacityProofError("INCOMPLETE_READ_WORKLOAD")
    if type(value["export_exit_code"]) is not int or value["export_exit_code"] != 0:
        raise CapacityProofError("GENUINE_EXPORT_FAILED_OR_MISSING")
    if (
        type(value["external_exchange_requests_forced"]) is not int
        or value["external_exchange_requests_forced"] != 0
    ):
        raise CapacityProofError("FORCED_EXTERNAL_WORKLOAD")
    exact_int(value["coverage_sample_count"], minimum=1, maximum=10000)
    coverage = value["minimum_coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {
        "adapters",
        "venue_groups",
        "books",
    }:
        raise CapacityProofError("INCOMPLETE_COVERAGE_EVIDENCE")
    for key, minimum in (("adapters", 20), ("venue_groups", 19), ("books", 200)):
        if exact_int(coverage[key], maximum=100000) < minimum:
            raise CapacityProofError("MARKET_COVERAGE_REDUCED")
    return peak


def validate_capacity_proof(proof):
    expected = {
        "schema_version",
        "kind",
        "before",
        "after",
        "evidence_sha256",
        "planning_envelope_mib",
        "reserve_mib",
        "startup_margin_mib",
        "minimum_after_seconds",
        "minimum_peak_saving_mib",
        "peak_saving_mib",
        "cold_cache_savings_proven",
        "sustained_vps_coexistence_proven",
        "proof_sha256",
    }
    if not isinstance(proof, dict) or set(proof) != expected or proof["schema_version"] != SCHEMA:
        raise CapacityProofError("INVALID_PROOF_STRUCTURE")
    if proof["kind"] != "DERIVED_ACTUAL_LOCAL_CAPACITY_EVIDENCE":
        raise CapacityProofError("UNSUPPORTED_CAPACITY_EVIDENCE")
    if len(canonical(proof)) > MAX_PROOF_BYTES:
        raise CapacityProofError("EVIDENCE_TOO_LARGE")
    if proof.get("proof_sha256") != seal_proof(proof)["proof_sha256"]:
        raise CapacityProofError("PROOF_DIGEST_MISMATCH")
    for key, expected_value in (
        ("reserve_mib", RESERVE_MIB),
        ("startup_margin_mib", STARTUP_MARGIN_MIB),
        ("minimum_after_seconds", MIN_AFTER_SECONDS),
        ("minimum_peak_saving_mib", MIN_PEAK_SAVING_MIB),
    ):
        if type(proof[key]) is not int or proof[key] != expected_value:
            raise CapacityProofError("SAFETY_POLICY_CHANGED")
    if (
        proof["cold_cache_savings_proven"] is not False
        or proof["sustained_vps_coexistence_proven"] is not False
    ):
        raise CapacityProofError("UNSUPPORTED_CAPACITY_CLAIM")
    hashes = proof["evidence_sha256"]
    if (
        not isinstance(hashes, dict)
        or set(hashes) != {"before", "after"}
        or any(
            not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
            for value in hashes.values()
        )
    ):
        raise CapacityProofError("INVALID_EVIDENCE_DIGEST")
    before_peak = validate_summary(proof["before"], after=False)
    after_peak = validate_summary(proof["after"], after=True)
    with localcontext() as context:
        context.prec = 80
        saving = before_peak - after_peak
    if saving < MIN_PEAK_SAVING_MIB or decimal_value(proof["peak_saving_mib"]) != saving:
        raise CapacityProofError("PEAK_SAVING_BELOW_200_MIB")
    envelope = planning_envelope(after_peak)
    if (
        type(proof["planning_envelope_mib"]) is not int
        or proof["planning_envelope_mib"] != envelope
    ):
        raise CapacityProofError("PLANNING_FORMULA_MISMATCH")
    return envelope


def capacity_policy(proof_base64=None):
    result = {
        "mode": "default_unproven_budget",
        "planning_envelope_mib": DEFAULT_PLANNING_MIB,
        "reserve_mib": RESERVE_MIB,
        "startup_margin_mib": STARTUP_MARGIN_MIB,
        "required_memavailable_mib": DEFAULT_PLANNING_MIB + RESERVE_MIB + STARTUP_MARGIN_MIB,
        "proof_valid": None,
        "cold_cache_savings_proven": False,
        "sustained_vps_coexistence_proven": False,
    }
    if proof_base64 is None:
        return result
    try:
        if not isinstance(proof_base64, str) or len(proof_base64) > MAX_PROOF_BASE64_BYTES:
            raise CapacityProofError("EVIDENCE_TOO_LARGE")
        try:
            decoded = base64.b64decode(proof_base64, validate=True)
        except (ValueError, TypeError):
            raise CapacityProofError("INVALID_BASE64_PROOF") from None
        proof = bounded_json(decoded, MAX_PROOF_BYTES)
        envelope = validate_capacity_proof(proof)
        result.update(
            mode="measured_local_budget",
            planning_envelope_mib=envelope,
            required_memavailable_mib=envelope + RESERVE_MIB + STARTUP_MARGIN_MIB,
            proof_valid=True,
            evidence=proof,
        )
    except (CapacityProofError, TypeError, KeyError, InvalidOperation) as error:
        result.update(
            proof_valid=False,
            error_code=str(error)
            if isinstance(error, CapacityProofError)
            else "INVALID_PROOF_STRUCTURE",
        )
    return result


def call(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)


def memory():
    return {
        line.split(":")[0]: int(line.split()[1])
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines()
        if len(line.split()) > 1 and line.split()[1].isdigit()
    }


def docker_data(args):
    response = call([*LOCAL_DOCKER, *args])
    if response.returncode:
        raise CapacityProofError("LOCAL_DOCKER_INSPECTION_FAILED")
    return response.stdout


