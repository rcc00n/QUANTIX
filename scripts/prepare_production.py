#!/usr/bin/env python3
"""Create distinct production credentials locally without printing or overwriting them."""

import hashlib
import json
import os
import secrets
from pathlib import Path

from credentials import generate_passphrase, password_hash

ROOT = Path(__file__).resolve().parents[1]


def put(path, value):
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise SystemExit("Existing production secrets must be private regular files")
        return
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value.rstrip("\n") + "\n")


def validate_bundle(directory):
    """Reject partially mismatched credentials without rotating a deployed identity."""
    try:
        password = (directory / "admin-password").read_text().strip()
        kind, salt, digest = (directory / "admin-password-hash").read_text().strip().split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=16384, r=8, p=1, dklen=64)
        if kind != "scrypt" or not secrets.compare_digest(actual.hex(), digest):
            raise ValueError
        for role, name, secret in (
            ("quantix_runtime", "runtime-database-url", "pg-runtime-password"),
            ("quantix_migrator", "migration-database-url", "pg-owner-password"),
        ):
            expected = (f"postgresql+asyncpg://{role}:"
                        f"{(directory / secret).read_text().strip()}@db:5432/quantix")
            if not secrets.compare_digest((directory / name).read_text().strip(), expected):
                raise ValueError
    except (OSError, ValueError, TypeError):
        raise SystemExit("Production credential bundle is inconsistent; no values overwritten") from None


def main():
    directory = ROOT / ".secrets/production"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    put(directory / "admin-password", generate_passphrase())
    password = (directory / "admin-password").read_text().strip()
    local = (ROOT / ".secrets/admin-password").read_text().strip()
    if secrets.compare_digest(password, local):
        raise SystemExit("Production credentials must differ from local credentials")
    put(directory / "admin-password-hash", password_hash(password))
    put(directory / "pg-owner-password", secrets.token_hex(32))
    put(directory / "pg-runtime-password", secrets.token_hex(32))
    put(directory / "internal-token", secrets.token_hex(32))
    for role, filename, secret in (
        ("quantix_runtime", "runtime-database-url", "pg-runtime-password"),
        ("quantix_migrator", "migration-database-url", "pg-owner-password"),
    ):
        db_password = (directory / secret).read_text().strip()
        put(directory / filename, f"postgresql+asyncpg://{role}:{db_password}@db:5432/quantix")
    values = {
        "DATABASE_URL_FILE": "/run/secrets/runtime_database_url",
        "ADMIN_PASSWORD_HASH_FILE": "/run/secrets/owner_password_hash",
        "COLLECTOR_INTERNAL_TOKEN_FILE": "/run/secrets/internal_token",
        "ADMIN_USERNAME": "quantix", "DEPLOYMENT_ENVIRONMENT": "production",
        "COOKIE_SECURE": "true", "ALLOWED_ORIGINS": '["https://quantix.raccncode.com"]',
        "PUBLIC_APP_URL": "https://quantix.raccncode.com", "TRADING_MODE": "paper",
        "PRODUCTION_DOMAIN": "quantix.raccncode.com", "COLLECTOR_URL": "http://collector:8001",
        "AI_ENABLED": "false", "AI_FREE_ALLOWANCE_AUTHORIZED": "false",
        "NOTIFICATION_WORKER_ENABLED": "false", "COLLECTOR__MAX_CONCURRENCY": "4",
        "COLLECTOR__MAX_MARKETS_PER_EXCHANGE": "10",
        "ARBITRAGE__MAX_OPTIMIZER_EVALUATIONS": "128",
    }
    put(directory / "runtime.env", "\n".join(f"{k}={v}" for k, v in values.items()))
    validate_bundle(directory)
    print(json.dumps({"production_credentials": "prepared privately; values suppressed",
                      "directory": ".secrets/production", "distinct_local_password": True}))


if __name__ == "__main__":
    main()
