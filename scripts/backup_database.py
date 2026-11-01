#!/usr/bin/env python3
# ruff: noqa: UP017 -- Local host and audited Ubuntu22.04 use Python3.10.
"""Private consistent pg_dump archive. No passwords on argv or in output metadata."""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rotate_backups(directory: Path, keep: int):
    """Rotate only self-identified duplicate recovery archives, never application data."""
    archives = []
    for meta in directory.glob("quantix-*.dump.json"):
        archive = meta.with_suffix("")
        if not re.fullmatch(r"quantix-\d{8}T\d{6}Z\.dump", archive.name):
            continue
        if any(
            p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o077
            for p in (meta, archive)
        ):
            continue
        try:
            value = json.loads(meta.read_text())
            valid = (
                value.get("kind") == "PRIVATE_POSTGRESQL_BACKUP"
                and value.get("production") is True
                and value.get("archive") == archive.name
                and value.get("bytes") == archive.stat().st_size
                and re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
            )
        except (OSError, ValueError, TypeError):
            continue
        if valid:
            archives.append((archive, meta))
    for archive, meta in sorted(archives, key=lambda pair: pair[0].name, reverse=True)[
        keep:
    ]:
        archive.unlink()
        meta.unlink()


