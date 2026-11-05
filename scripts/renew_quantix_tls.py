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


def validate_lineage():
    for path in (CLI_CONFIG, RENEWAL):
        try:
            info = path.lstat()
        except OSError:
            raise GuardError("QUANTIX_TLS_CONFIG_UNAVAILABLE") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise GuardError("QUANTIX_TLS_CONFIG_PERMISSIONS_INVALID")
    try:
        cli = CLI_CONFIG.read_text()
    except OSError:
        raise GuardError("QUANTIX_PRIVATE_CLI_CONFIG_REQUIRED") from None
    if len(cli) > 4096 or any(
        line.strip() and not line.lstrip().startswith(("#", ";"))
        for line in cli.splitlines()
    ):
        raise GuardError("QUANTIX_CLI_CONFIG_MUST_BE_EMPTY")
    try:
        text = RENEWAL.read_text()
    except OSError:
        raise GuardError("QUANTIX_RENEWAL_CONFIG_UNAVAILABLE") from None
    if len(text) > 65536:
        raise GuardError("QUANTIX_RENEWAL_CONFIG_TOO_LARGE")
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")) or "=" not in stripped:
            continue
        key, value = (p.strip() for p in stripped.split("=", 1))
        if key in values:
            raise GuardError("QUANTIX_RENEWAL_CONFIG_AMBIGUOUS")
        values[key] = value
    if values.get("authenticator") != "webroot" or values.get(
        "installer", "None"
    ) not in ("None", ""):
        raise GuardError("QUANTIX_RENEWAL_MUST_USE_WEBROOT")
    if any(
        values.get(key)
        for key in ("pre_hook", "post_hook", "deploy_hook", "renew_hook")
    ):
        raise GuardError("QUANTIX_RENEWAL_HOOKS_NOT_ALLOWED")
    expected_paths = {
        "archive_dir": CONFIG / "archive" / DOMAIN,
        "cert": CONFIG / "live" / DOMAIN / "cert.pem",
        "privkey": CONFIG / "live" / DOMAIN / "privkey.pem",
        "chain": CONFIG / "live" / DOMAIN / "chain.pem",
        "fullchain": FULLCHAIN,
    }
    for key, expected in expected_paths.items():
        supplied = Path(values.get(key, "/invalid"))
        if supplied != expected or not supplied.resolve().is_relative_to(
            CONFIG.resolve()
        ):
            raise GuardError("QUANTIX_RENEWAL_PATH_NOT_ISOLATED")
    for key in ("webroot_path", DOMAIN):
        if values.get(key, "").rstrip(",").strip().strip("\"'") != str(WEBROOT):
            raise GuardError("QUANTIX_WEBROOT_NOT_ISOLATED")
    if values.get("server") != "https://acme-v02.api.letsencrypt.org/directory":
        raise GuardError("QUANTIX_RENEWAL_CA_NOT_PRODUCTION_LETSENCRYPT")
    certificate = run(
        ["openssl", "x509", "-in", str(FULLCHAIN), "-noout", "-ext", "subjectAltName"]
    )
    names = set(re.findall(r"DNS:([^,\s]+)", certificate.stdout))
    if (
        certificate.returncode
        or names != {DOMAIN}
        or "IP Address:" in certificate.stdout
    ):
        raise GuardError("QUANTIX_CERTIFICATE_NAMES_NOT_EXCLUSIVE")


def renew(baseline, *, apply=False):
    checksum = baseline.get("quantix_vhost_sha256")
    checked_reload(baseline, checksum, apply=False)
    validate_lineage()
    before = certificate_hash()
    pending = pending_hash()
    if pending is not None and pending != before:
        raise GuardError("PENDING_CERTIFICATE_HASH_MISMATCH")
    if not apply:
        return {
            "status": "renewal_checks_passed",
            "applied": False,
            "certificate_changed": False,
            "pending_reload": pending is not None,
        }
    result = run(
        [
            "certbot",
            "renew",
            "--config",
            str(CLI_CONFIG),
            "--cert-name",
            DOMAIN,
            "--non-interactive",
            "--no-random-sleep-on-renew",
            "--no-directory-hooks",
            "--config-dir",
            str(CONFIG),
            "--work-dir",
            str(WORK),
            "--logs-dir",
            str(LOGS),
        ],
        timeout=180,
    )
    after = certificate_hash()
    if before != after:
        mark_pending(
            after
        )  # Survives a blocked reload or interruption; no extra issuance required.
    if result.returncode:
        raise GuardError("QUANTIX_CERTBOT_RENEWAL_FAILED")
    if before == after and pending is None:
        return {
            "status": "certificate_unchanged",
            "applied": True,
            "certificate_changed": False,
        }
    validate_lineage()
    guard = checked_reload(baseline, checksum, apply=True)
    try:
        pending_path().unlink(missing_ok=True)
    except OSError:
        raise GuardError(
            "RELOADED_CERTIFICATE_STATE_CLEANUP_FAILED", reload_applied=True
        ) from None
    return {
        "status": "certificate_renewed_and_reloaded",
        "applied": True,
        "certificate_changed": before != after,
        "pending_reload_recovered": pending is not None,
        "reload": guard,
    }


