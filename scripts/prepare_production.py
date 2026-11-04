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


