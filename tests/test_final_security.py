"""SYNTHETIC credentials only; runtime secret and production fail-closed invariants."""

from datetime import UTC, datetime, timedelta

import pytest
from arbiq.core.config import Settings
from arbiq.core.logging import redact_sensitive, redact_text
from arbiq.core.secrets import read_secret_file, secret_environment
from pydantic import ValidationError

HASH = "scrypt$" + "ab" * 16 + "$" + "cd" * 64


def settings(**values):
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://synthetic:synthetic@db/synthetic_test",
        admin_password_hash=HASH,
        **values,
    )


def test_secret_file_precedence_and_missing_file_fail_closed(tmp_path, monkeypatch):
    p = tmp_path / "synthetic-secret"
    p.write_text("synthetic-file-value\n")
    monkeypatch.setenv("SYNTHETIC_SECRET", "synthetic-env-value")
    monkeypatch.setenv("SYNTHETIC_SECRET_FILE", str(p))
    assert secret_environment("SYNTHETIC_SECRET") == "synthetic-file-value"
    p.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        secret_environment("SYNTHETIC_SECRET")


@pytest.mark.parametrize("content", ["", "a\nb", "a\x00b", "x" * 16385])
def test_secret_file_rejects_invalid_content(tmp_path, content):
    p = tmp_path / "synthetic-secret"
    p.write_text(content)
    with pytest.raises(ValueError):
        read_secret_file(str(p))


def test_config_can_load_protected_credentials_without_environment_values(tmp_path):
    db = tmp_path / "database"
    db.write_text("postgresql+asyncpg://synthetic:synthetic@db/synthetic_test")
    pw = tmp_path / "hash"
    pw.write_text(HASH)
    config = Settings(_env_file=None, database_url_file=str(db), admin_password_hash_file=str(pw))
    assert config.database_url.get_secret_value().endswith("synthetic_test")
    assert HASH not in repr(config)






