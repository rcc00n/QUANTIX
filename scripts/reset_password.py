#!/usr/bin/env python3
"""Rotate the local operator credential; restart backend to revoke old sessions."""

import re
from pathlib import Path

from credentials import generate_passphrase, password_hash, protected_write

ROOT = Path(__file__).resolve().parents[1]


def reset() -> None:
    env = ROOT / ".env"
    if not env.exists():
        raise SystemExit("Run make setup first")
    text = env.read_text()
    match = re.search(r"^ADMIN_USERNAME=(.+)$", text, flags=re.MULTILINE)
    previous = match.group(1).strip("'\"") if match else "admin"
    password = generate_passphrase()
    values = {
        "ADMIN_USERNAME": "quantix",
        "ADMIN_PASSWORD_HASH": f"'{password_hash(password)}'",
    }
    if previous != "quantix":
        values["ADMIN_PREVIOUS_USERNAME"] = previous
    for key, value in values.items():
        if re.search(rf"^{key}=", text, flags=re.MULTILINE):
            text = re.sub(
                rf"^{key}=.*$",
                lambda _, replacement=f"{key}={value}": replacement,
                text,
                flags=re.MULTILINE,
            )
        else:
            text += f"\n{key}={value}\n"
    directory = ROOT / ".secrets"
    directory.mkdir(exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    protected_write(directory / "admin-password", password + "\n")
    protected_write(env, text)
    print(
        "QUANTIX credential rotated. Username: quantix. Password saved in .secrets/admin-password (0600)."
    )
    print(
        "Restart backend to apply the hash and revoke prior sessions. No password was logged."
    )


if __name__ == "__main__":
    reset()
