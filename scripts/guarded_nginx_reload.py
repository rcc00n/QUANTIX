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


def modified_at(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        raise GuardError("CONFIG_FILE_UNAVAILABLE") from None


def protected_tls_references(config_text):
    """Literal file references only. Do not read or log certificate/key contents."""
    lexer = shlex.shlex(config_text, posix=True, punctuation_chars="{};")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    lexer.escape = ""
    statement = []
    references = set()
    try:
        for token in lexer:
            parts = list(token) if token and all(c in "{};" for c in token) else [token]
            for part in parts:
                if part in ("{", "}", ";"):
                    if statement and statement[0] in (
                        "ssl_certificate",
                        "ssl_certificate_key",
                    ):
                        if part != ";" or len(statement) != 2:
                            raise GuardError("TLS_REFERENCE_REQUIRES_INSPECTION")
                        value = statement[1]
                        if not value.startswith("/") or "$" in value or "\\" in value:
                            raise GuardError("TLS_REFERENCE_REQUIRES_INSPECTION")
                        path = Path(value)
                        if path.is_relative_to(QUANTIX_CERTBOT):
                            if not path.resolve().is_relative_to(
                                QUANTIX_CERTBOT.resolve()
                            ):
                                raise GuardError("TLS_REFERENCE_REQUIRES_INSPECTION")
                        else:
                            references.add(value)
                    statement = []
                else:
                    statement.append(part)
    except (OSError, ValueError):
        raise GuardError("TLS_REFERENCE_REQUIRES_INSPECTION") from None
    return references


def tls_modified_at(path):
    try:
        reference = Path(path)
        target, link = reference.stat(), reference.lstat()
        if not stat.S_ISREG(target.st_mode):
            raise GuardError("PROTECTED_TLS_MATERIAL_UNAVAILABLE")
        # A retargeted certificate symlink also represents a pending change.
        return max(target.st_mtime, link.st_mtime)
    except OSError:
        raise GuardError("PROTECTED_TLS_MATERIAL_UNAVAILABLE") from None


def master_pid():
    result = run(["systemctl", "show", "nginx", "-p", "MainPID", "--value"])
    if (
        result.returncode
        or not result.stdout.strip().isdigit()
        or int(result.stdout.strip()) <= 1
    ):
        raise GuardError("NGINX_MASTER_UNAVAILABLE")
    return int(result.stdout.strip())


def worker_generation(master):
    result = run(["pgrep", "-P", str(master), "-x", "nginx"])
    if result.returncode:
        raise GuardError("NGINX_WORKERS_UNAVAILABLE")
    starts = []
    for pid in result.stdout.split():
        if not pid.isdigit():
            raise GuardError("NGINX_WORKER_PID_INVALID")
        try:
            raw = Path("/proc", pid, "stat").read_text()
            fields = raw[raw.rfind(")") + 2 :].split()
            if int(fields[1]) == master:
                starts.append(int(fields[19]))
        except (OSError, ValueError, IndexError):
            continue  # A draining worker can exit during this read.
    if not starts:
        raise GuardError("NGINX_WORKERS_UNAVAILABLE")
    return max(starts)


def worker_started_at(start_ticks):
    boot = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    return boot + start_ticks / os.sysconf("SC_CLK_TCK")


def protected_https():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    allowed = {
        "badguymotors.com": {
            "badguymotors.com",
            "www.badguymotors.com",
            "badguymotors.ca",
            "www.badguymotors.ca",
        },
        "raccncode.com": {"raccncode.com", "www.raccncode.com"},
    }
    for url in PROTECTED_HTTPS:
        try:
            with opener.open(url, timeout=5) as response:
                final = urllib.parse.urlsplit(response.geturl())
                if (
                    response.status != 200
                    or final.scheme != "https"
                    or final.hostname
                    not in allowed[urllib.parse.urlsplit(url).hostname]
                ):
                    raise GuardError("PROTECTED_HTTPS_NOT_HEALTHY")
        except GuardError:
            raise
        except (OSError, TimeoutError, ValueError):
            raise GuardError("PROTECTED_HTTPS_NOT_HEALTHY") from None


@dataclass(frozen=True)
class Snapshot:
    master: int
    worker_started: float
    generation_ticks: int
    files: tuple[str, ...]
    hashes: dict[str, str]


def inspect(baseline, expected_vhost_sha256):
    if not isinstance(expected_vhost_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_vhost_sha256
    ):
        raise GuardError("REVIEWED_VHOST_CHECKSUM_REQUIRED")
    clock = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if clock.returncode or clock.stdout.strip() != "yes":
        raise GuardError("VPS_CLOCK_NOT_SYNCHRONIZED")
    result = run(["nginx", "-T"])
    if result.returncode:
        raise GuardError("NGINX_SYNTAX_INVALID")
    prefix = "# configuration file "
    files = [
        line[len(prefix) : -1]
        for line in result.stdout.splitlines()
        if line.startswith(prefix) and line.endswith(":")
    ]
    if files.count(VHOST) != 1 or len(files) != len(set(files)):
        raise GuardError("EXACTLY_ONE_QUANTIX_VHOST_REQUIRED")
    if [p for p in files if p != VHOST] != baseline["loaded_files_in_order"]:
        raise GuardError("PROTECTED_INCLUDE_ORDER_CHANGED")
    if warning_counts(warnings(result.stderr)) != warning_counts(baseline["warnings"]):
        raise GuardError("NGINX_WARNING_MULTISET_CHANGED")
    hashes = {path: digest(path) for path in files}
    if hashes[VHOST] != expected_vhost_sha256:
        raise GuardError("QUANTIX_VHOST_NOT_REVIEWED")
    if any(hashes[p] != h for p, h in baseline["loaded_file_sha256"].items()):
        raise GuardError("PROTECTED_CONFIG_HASH_CHANGED")
    master = master_pid()
    generation = worker_generation(master)
    started_at = worker_started_at(generation)
    for path in baseline["loaded_files_in_order"]:
        try:
            if modified_at(path) > started_at:
                raise GuardError("PENDING_UNRELATED_CONFIG_EDIT")
        except OSError:
            raise GuardError("CONFIG_FILE_UNAVAILABLE") from None
    for reference in protected_tls_references(result.stdout):
        if tls_modified_at(reference) > started_at:
            raise GuardError("PENDING_PROTECTED_TLS_MATERIAL")
    return Snapshot(master, started_at, generation, tuple(files), hashes)


