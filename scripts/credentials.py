"""Local credential helpers. Never print generated passwords or hashes."""

import hashlib
import os
import secrets
import tempfile
from pathlib import Path


def generate_passphrase() -> str:
    words = [
        line.split()[1]
        for line in Path(__file__)
        .with_name("passphrase_words.txt")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    if len(set(words)) < 1296:
        raise RuntimeError("Passphrase dictionary is incomplete")
    # Six independent EFF short-list words + four unambiguous digits: >74 bits.
    return (
        "-".join(secrets.choice(words) for _ in range(6))
        + "-"
        + "".join(secrets.choice("23456789") for _ in range(4))
    )


