#!/usr/bin/env python3
"""Check QUANTIX's additive NGINX host; only --apply may send one graceful HUP.

The root-owned baseline pins the existing loaded file order, SHA-256 values and
warning multiset. A later unrelated change fails closed. Rebaselining requires
independent inspection; this helper never rewrites a baseline or restarts NGINX.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

BASELINE = Path("/opt/quantix/safety/nginx-baseline.json")
VHOST = "/etc/nginx/conf.d/zz-quantix.conf"
MAX_OUTPUT = 4 * 1024 * 1024
PROTECTED_HTTPS = ("https://badguymotors.com", "https://raccncode.com")
QUANTIX_CERTBOT = Path("/opt/quantix/certbot")


class GuardError(Exception):
    def __init__(self, code, *, reload_applied=False):
        self.code, self.reload_applied = code, reload_applied
        super().__init__(code)


def run(args, *, timeout=20):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GuardError("COMMAND_FAILED") from None
    if len(result.stdout) + len(result.stderr) > MAX_OUTPUT:
        raise GuardError("COMMAND_OUTPUT_LIMIT")
    return result


def digest(path):
    try:
        value = Path(path).read_bytes()
    except OSError:
        raise GuardError("CONFIG_FILE_UNAVAILABLE") from None
    if len(value) > MAX_OUTPUT:
        raise GuardError("CONFIG_FILE_SIZE_LIMIT")
    return hashlib.sha256(value).hexdigest()


def load_baseline(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise GuardError("BASELINE_PERMISSIONS_INVALID")
        if info.st_uid != os.geteuid():
            raise GuardError("BASELINE_OWNER_INVALID")
        if info.st_size > 256 * 1024:
            raise GuardError("BASELINE_SIZE_LIMIT")
        baseline = json.loads(path.read_text())
        files, hashes = (
            baseline["loaded_files_in_order"],
            baseline["loaded_file_sha256"],
        )
        valid = (
            isinstance(files, list)
            and 0 < len(files) <= 256
            and all(isinstance(p, str) and p.startswith("/") for p in files)
            and len(files) == len(set(files))
            and VHOST not in files
            and isinstance(hashes, dict)
            and set(hashes) == set(files)
            and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes.values())
            and isinstance(baseline["warnings"], list)
        )
        if not valid:
            raise GuardError("BASELINE_FORMAT_INVALID")
        return baseline
    except GuardError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise GuardError("BASELINE_UNAVAILABLE_OR_INVALID") from None


def warnings(stderr):
    result = []
    for line in stderr.splitlines():
        if "[warn]" not in line:
            continue
        conflict = re.search(
            r'conflicting server name "([^"]+)" on ([^,]+), ignored', line
        )
        if conflict:
            result.append(
                {
                    "type": "conflicting_server_name",
                    "name": conflict[1],
                    "listen": conflict[2],
                }
            )
        else:
            message = re.sub(r"^\s*\d+#\d+:\s*", "", line.split("[warn]", 1)[1])
            result.append({"type": "warning", "message": message.strip()})
    return result


def warning_counts(items):
    return Counter(
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in items
    )


