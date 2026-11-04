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


