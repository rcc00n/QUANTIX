#!/usr/bin/env python3
"""Estimate a disk growth scenario from comparable PostgreSQL size samples."""

import argparse
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

DAY_US = 86_400_000_000
MAX_SAMPLES = 10_000
SIZE_FIELDS = ("database_bytes", "evaluation_bytes", "book_snapshot_bytes")
COUNT_FIELDS = ("ledger_rows", "evaluation_rows")
UTC_PATTERN = re.compile(r"([0-9-]{10}[T ][0-9:]{8})(?:\.(\d{1,6}))?(?:Z|\+00:00)")


def microseconds(span):
    return (span.days * 86_400 + span.seconds) * 1_000_000 + span.microseconds


def estimate(samples, *, free_bytes=None, reserve_bytes=None):
    if not 2 <= len(samples) <= MAX_SAMPLES:
        raise ValueError("At least two comparable observations are required")
    source = samples[0].get("database_scope")
    if not isinstance(source, str) or not source:
        raise ValueError("A database scope is required")
    observations = []
    for sample in samples:
        if sample.get("database_scope") != source:
            raise ValueError("Database scopes differ")
        stamp = UTC_PATTERN.fullmatch(sample["observed_at"])
        if not stamp:
            raise ValueError("Observation timestamps must be UTC")
        # PostgreSQL can omit trailing zeroes; Python 3.10 needs six digits here.
        observed = datetime.fromisoformat(
            f"{stamp[1]}.{(stamp[2] or '').ljust(6, '0')}+00:00"
        )
        if observations and observed <= observations[-1]:
            raise ValueError("Observation timestamps must increase strictly")
        if any(
            type(sample.get(key)) is not int or sample[key] < 0
            for key in (*SIZE_FIELDS, *COUNT_FIELDS)
        ):
            raise ValueError("Size and row counters must be nonnegative integers")
        observations.append(observed)
    if (free_bytes is None) != (reserve_bytes is None):
        raise ValueError(
            "Specify both free bytes and the existing disk reserve, or neither"
        )
    if any(
        x is not None and (type(x) is not int or x < 0)
        for x in (free_bytes, reserve_bytes)
    ):
        raise ValueError("Disk capacity values must be nonnegative integers")

    elapsed = microseconds(observations[-1] - observations[0])
    first, last = samples[0], samples[-1]
    deltas = {key: last[key] - first[key] for key in (*SIZE_FIELDS, *COUNT_FIELDS)}
    daily = Decimal(deltas["database_bytes"]) * DAY_US / elapsed
    rates = [
        Decimal(b["database_bytes"] - a["database_bytes"])
        * DAY_US
        / microseconds(y - x)
        for a, b, x, y in zip(
            samples, samples[1:], observations, observations[1:], strict=False
        )
    ]
    available = max(0, free_bytes - reserve_bytes) if free_bytes is not None else None
    runway = Decimal(available) / daily if available is not None and daily > 0 else None
    return {
        "source": source,
        "sample_count": len(samples),
        "first_at": observations[0].isoformat(),
        "last_at": observations[-1].isoformat(),
        "elapsed_seconds": str(Decimal(elapsed) / 1_000_000),
        "start_database_bytes": first["database_bytes"],
        "end_database_bytes": last["database_bytes"],
        "observed_deltas": deltas,
        "linear_database_bytes_per_day": str(daily),
        "interval_minimum_bytes_per_day": str(min(rates)),
        "interval_maximum_bytes_per_day": str(max(rates)),
        "linear_30_day_database_growth_bytes": str(daily * 30) if daily > 0 else None,
        "filesystem_free_bytes": free_bytes,
        "filesystem_reserve_bytes": reserve_bytes,
        "linear_database_only_runway_days": str(runway) if runway is not None else None,
        "limitations": [
            "This scenario extrapolates the observed database-size delta only",
            "WAL, logs, exports, backups, other workloads and filesystem reclamation are separate",
            "A short observation window cannot certify 30 days of retention",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--free-bytes", type=int)
    parser.add_argument("--reserve-bytes", type=int)
    args = parser.parse_args()
    with args.observations.open() as source:
        samples = []
        for line in source:
            if len(samples) == MAX_SAMPLES:
                raise SystemExit("Observation file exceeds bounded sample count")
            samples.append(json.loads(line))
    try:
        print(
            json.dumps(
                estimate(
                    samples,
                    free_bytes=args.free_bytes,
                    reserve_bytes=args.reserve_bytes,
                ),
                indent=2,
            )
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
