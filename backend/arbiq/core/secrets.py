"""Read explicitly configured runtime secret files without logging their values."""

import os
from pathlib import Path


def read_secret_file(filename: str) -> str:
    path = Path(filename)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Runtime secret file is unavailable")
    if path.stat().st_size > 16384:
        raise ValueError("Runtime secret file exceeds the size limit")
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeError):
        raise ValueError("Runtime secret file could not be read") from None
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("Runtime secret file must contain one nonempty line")
    return value


def secret_environment(name: str) -> str:
    """FILE takes precedence; an explicitly missing file must fail closed."""
    filename = os.environ.get(f"{name}_FILE")
    if filename:
        return read_secret_file(filename)
    value = os.environ.get(name)
    if not value:
        raise ValueError("Required runtime credential is unavailable")
    return value
