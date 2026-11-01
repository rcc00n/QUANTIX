#!/usr/bin/env python3
"""Read a genuine analytical ZIP and compare retained sizes without database writes.

This accepts retained exports only; it neither contacts exchanges nor generates
paper executions. SYNTHETIC archives remain explicitly labeled by their source.
"""

import argparse
import csv
import hashlib
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from arbiq.opportunities.diagnostics import size_diagnostics

MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ROWS = 100000


def audit(path: Path, evaluation_id: UUID, budget: Decimal):
    csv.field_size_limit(4 * 1024 * 1024)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("opportunities.csv")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError("Retained opportunity member exceeds audit size limit")
        with archive.open(info) as stream:
            reader = csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8"))
            for index, row in enumerate(reader):
                if index >= MAX_ROWS:
                    raise ValueError("Retained opportunities exceed audit row limit")
                if row["id"] != str(evaluation_id):
                    continue
                provenance = json.loads(row["provenance_json"])
                evaluation = provenance["evaluation"]
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    while chunk := source.read(65536):
                        digest.update(chunk)
                return {
                    "source_archive": path.name,
                    "source_sha256": digest.hexdigest(),
                    "source_kind": provenance["kind"],
                    "evaluation_id": evaluation["id"],
                    "route_id": evaluation["route_id"],
                    **size_diagnostics(evaluation["evidence"], budget),
                }
    raise ValueError("Requested evaluation not present in retained archive")


