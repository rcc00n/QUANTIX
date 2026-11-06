#!/usr/bin/env python3
# ruff: noqa: UP017 -- The production host runs Python 3.10.
"""Controlled QUANTIX image replacement. Use only after the documented global gates."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

import production_preflight as gate

ROOT = gate.PRODUCTION_ROOT
PRIVATE = ROOT / ".local/stage8a1"
BASE = ROOT / "infra/compose.production.yaml"
COMPOSE = [*gate.LOCAL_DOCKER, "compose", "--project-name", "quantix", "-f", str(BASE)]
OVERLAYS = {
    "stage8a1": {
        "candidate": "compose.stage8a.candidate.yaml",
        "rollback": "compose.stage8a.rollback.yaml",
    },
    "stage8b1": {
        "candidate": "compose.stage8b1.candidate.yaml",
        "rollback": "compose.stage8b1.rollback.yaml",
    },
    "stage8c": {
        "candidate": "compose.stage8c.candidate.yaml",
        "rollback": "compose.stage8c.rollback.yaml",
    },
}
EXPORT_MOUNT = "/var/tmp/quantix-export"


def checked(args, *, timeout=20):
    completed = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if completed.returncode:
        raise gate.CapacityProofError("COMMAND_FAILED")
    return completed.stdout


