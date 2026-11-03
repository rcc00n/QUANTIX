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


def summarize_profile(profile):
    if profile.get("kind") != PROFILE_KIND:
        raise CapacityProofError("UNSUPPORTED_PROFILE_KIND")
    samples = profile.get("samples")
    if not isinstance(samples, list) or not 2 <= len(samples) <= 10000:
        raise CapacityProofError("INCOMPLETE_SERVICE_MEASUREMENT")
    service_peaks = {role: Decimal(0) for role in REQUIRED_ROLES}
    instance_ids = {}
    times, totals = [], []
    for sample in samples:
        if not isinstance(sample, dict):
            raise CapacityProofError("INVALID_SAMPLE")
        times.append(decimal_value(sample.get("elapsed_seconds")))
        services = sample.get("services")
        if not isinstance(services, list) or len(services) != len(REQUIRED_ROLES):
            raise CapacityProofError("INCOMPLETE_SERVICE_MEASUREMENT")
        measured = {}
        for service in services:
            if not isinstance(service, dict):
                raise CapacityProofError("INVALID_SERVICE_SAMPLE")
            name = service.get("Name")
            role = next((r for r in REQUIRED_ROLES if name == f"arbiq-{r}-1"), None)
            if role is None or role in measured:
                raise CapacityProofError("INCOMPLETE_SERVICE_MEASUREMENT")
            instance = service.get("ID")
            if not isinstance(instance, str) or not re.fullmatch(
                r"[a-f0-9]{12,64}", instance
            ):
                raise CapacityProofError("INVALID_SERVICE_INSTANCE")
            if role in instance_ids and instance_ids[role] != instance:
                raise CapacityProofError("SERVICE_RESTART_DURING_MEASUREMENT")
            instance_ids[role] = instance
            measured[role] = docker_memory_mib(service.get("MemUsage"))
            service_peaks[role] = max(service_peaks[role], measured[role])
        with localcontext() as context:
            context.prec = 80
            totals.append(sum(measured.values(), Decimal(0)))
    if any(later <= earlier for earlier, later in itertools.pairwise(times)):
        raise CapacityProofError("NON_MONOTONIC_PROFILE_SAMPLES")
    reads = profile.get("reads")
    if (
        not isinstance(reads, list)
        or not len(REQUIRED_READ_CATEGORIES) <= len(reads) <= 10000
    ):
        raise CapacityProofError("INCOMPLETE_READ_WORKLOAD")
    categories = set()
    for item in reads:
        if (
            not isinstance(item, dict)
            or type(item.get("status")) is not int
            or item["status"] != 200
        ):
            raise CapacityProofError("NORMAL_READ_FAILED")
        categories.add(read_category(item.get("path")))
    coverage = profile.get("coverage")
    if not isinstance(coverage, list) or not 1 <= len(coverage) <= 10000:
        raise CapacityProofError("INCOMPLETE_COVERAGE_EVIDENCE")
    minimum_coverage = {}
    for source, target in (
        ("connected_exchanges", "adapters"),
        ("venue_groups", "venue_groups"),
        ("monitored_markets", "books"),
    ):
        values = []
        for entry in coverage:
            if not isinstance(entry, dict):
                raise CapacityProofError("INCOMPLETE_COVERAGE_EVIDENCE")
            values.append(exact_int(entry.get(source)))
        minimum_coverage[target] = min(values)
    return {
        "elapsed_seconds": str(decimal_value(profile.get("elapsed_seconds"))),
        "sample_count": len(samples),
        "sample_first_seconds": str(times[0]),
        "sample_last_seconds": str(times[-1]),
        "maximum_sample_gap_seconds": str(
            max(b - a for a, b in itertools.pairwise(times))
        ),
        "stack_peak_mib": str(max(totals)),
        "service_peak_mib": {role: str(service_peaks[role]) for role in REQUIRED_ROLES},
        "read_count": len(reads),
        "read_status_codes": [200],
        "read_categories": sorted(categories),
        "coverage_sample_count": len(coverage),
        "minimum_coverage": minimum_coverage,
        "export_exit_code": profile.get("export_exit_code"),
        "external_exchange_requests_forced": profile.get(
            "external_exchange_requests_forced"
        ),
        "stable_service_instances": True,
    }


def build_capacity_proof(before_bytes, after_bytes):
    before = summarize_profile(bounded_json(before_bytes, MAX_PROFILE_BYTES))
    after = summarize_profile(bounded_json(after_bytes, MAX_PROFILE_BYTES))
    with localcontext() as context:
        context.prec = 80
        saving = Decimal(before["stack_peak_mib"]) - Decimal(after["stack_peak_mib"])
    proof = seal_proof(
        {
            "schema_version": SCHEMA,
            "kind": "DERIVED_ACTUAL_LOCAL_CAPACITY_EVIDENCE",
            "before": before,
            "after": after,
            "evidence_sha256": {
                "before": hashlib.sha256(before_bytes).hexdigest(),
                "after": hashlib.sha256(after_bytes).hexdigest(),
            },
            "planning_envelope_mib": planning_envelope(after["stack_peak_mib"]),
            "reserve_mib": RESERVE_MIB,
            "startup_margin_mib": STARTUP_MARGIN_MIB,
            "minimum_after_seconds": MIN_AFTER_SECONDS,
            "minimum_peak_saving_mib": MIN_PEAK_SAVING_MIB,
            "peak_saving_mib": str(saving),
            "cold_cache_savings_proven": False,
            "sustained_vps_coexistence_proven": False,
        }
    )
    validate_capacity_proof(proof)
    return proof


def read_profile(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_PROFILE_BYTES + 1)
    except OSError:
        raise CapacityProofError("PROFILE_FILE_UNAVAILABLE") from None
    if len(data) > MAX_PROFILE_BYTES:
        raise CapacityProofError("EVIDENCE_TOO_LARGE")
    return data


