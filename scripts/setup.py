#!/usr/bin/env python3
"""Generate local credentials without printing them or overwriting existing secrets."""

import os
import secrets
from pathlib import Path

from credentials import generate_passphrase, password_hash

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    secret_dir = ROOT / ".secrets"
    secret_dir.mkdir(exist_ok=True, mode=0o700)
    secret_dir.chmod(0o700)
    telegram_secret = secret_dir / "telegram-bot-token"
    if not telegram_secret.exists():
        fd = os.open(telegram_secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    target = ROOT / ".env"
    if target.exists():
        print("Existing .env preserved. No credentials changed.")
        return
    password = generate_passphrase()
    db_password = secrets.token_hex(24)
    text = (ROOT / ".env.example").read_text()
    text = text.replace(
        "COLLECTOR_INTERNAL_TOKEN=\n",
        f"COLLECTOR_INTERNAL_TOKEN={secrets.token_hex(32)}\n",
    )
    text = text.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={db_password}\n")
    text = text.replace("REPLACE_WITH_GENERATED_PASSWORD", db_password)
    text = text.replace(
        "ADMIN_PASSWORD_HASH=\n", f"ADMIN_PASSWORD_HASH='{password_hash(password)}'\n"
    )
    for path, content in [
        (target, text),
        (secret_dir / "admin-password", password + "\n"),
    ]:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    print("Created .env (0600). Admin password: .secrets/admin-password (0600).")


if __name__ == "__main__":
    main()
