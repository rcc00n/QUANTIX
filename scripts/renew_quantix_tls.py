#!/usr/bin/env python3
"""Renew only QUANTIX's isolated webroot certificate, guarded and dry-run by default.

A changed unrelated configuration blocks before Certbot and before any reload.
Rebaseline only after independent inspection; never accept a current hash blindly.
No shared certbot hook, NGINX installer, service restart or other certificate is used.

Provision /opt/quantix/safety/nginx-validation-logs root:root 0700 and a comment-only
/opt/quantix/certbot/cli.ini before enabling the optional unit. Test the exact unit
namespace with this helper WITHOUT --apply first: standard NGINX test log opens are
bound to owned scratch logs, while unknown outside log paths fail read-only.
Synthetic tests do not establish that the real service or renewal has run.

A private pending certificate hash survives a blocked/interrupted reload. After an
interruption a later recovery may repeat one guarded graceful HUP; it never restarts
NGINX. Unrelated changes remain blocked until separately inspected and rebaselined.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from guarded_nginx_reload import (
    BASELINE,
    GuardError,
    checked_reload,
    load_baseline,
    run,
)

DOMAIN = "quantix.raccncode.com"
CONFIG = Path("/opt/quantix/certbot/config")
WORK = Path("/opt/quantix/certbot/work")
LOGS = Path("/opt/quantix/certbot/logs")
FULLCHAIN = CONFIG / "live" / DOMAIN / "fullchain.pem"
RENEWAL = CONFIG / "renewal" / f"{DOMAIN}.conf"
CLI_CONFIG = CONFIG.parent / "cli.ini"
WEBROOT = Path("/opt/quantix/acme-webroot")


def pending_path():
    return CONFIG.parent / "pending-reload.sha256"


def mark_pending(value):
    descriptor, temporary = tempfile.mkstemp(
        prefix=".pending-reload-", dir=CONFIG.parent
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, pending_path())
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pending_hash():
    try:
        value = pending_path().read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        raise GuardError("PENDING_CERTIFICATE_STATE_UNAVAILABLE") from None
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise GuardError("PENDING_CERTIFICATE_STATE_INVALID")
    return value


def certificate_hash():
    try:
        return hashlib.sha256(FULLCHAIN.read_bytes()).hexdigest()
    except OSError:
        raise GuardError("QUANTIX_CERTIFICATE_UNAVAILABLE") from None


