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


def test_production_requires_exact_https_origin_and_secure_cookie():
    secure = {
        "deployment_environment": "production", "cookie_secure": True,
        "public_app_url": "https://quantix.raccncode.com",
        "allowed_origins": ["https://quantix.raccncode.com"],
    }
    assert settings(**secure).cookie_secure
    for overrides in (
        {"cookie_secure": False},
        {"public_app_url": "http://quantix.raccncode.com"},
        {"allowed_origins": ["https://evil.example"]},
        {"public_app_url": "https://quantix.raccncode.com@evil.example"},
    ):
        with pytest.raises(ValidationError):
            settings(**(secure | overrides))


def test_ai_cannot_enable_without_explicit_current_free_authorization():
    for values in ({}, {"ai_free_allowance_authorized": True}):
        with pytest.raises(ValidationError):
            settings(ai_enabled=True, **values)
    with pytest.raises(ValidationError):
        settings(
            ai_enabled=True, ai_free_allowance_authorized=True,
            nvidia_api_key="synthetic-key", nvidia_model="synthetic-model",
            ai_free_allowance_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )


def test_tokens_and_nested_credentials_are_redacted():
    token = "123456789:" + "x" * 35
    assert token not in redact_text("https://api.telegram.org/bot" + token + "/getMe")
    event = redact_sensitive(None, "info", {
        "event": "provider failed", "nested": {"api_key": "synthetic-key"},
        "error": token, "chat_id": "synthetic-chat", "amount_cad": "50",
    })
    assert "synthetic-key" not in str(event)
    assert token not in str(event)
    assert "synthetic-chat" not in str(event)
    assert event["amount_cad"] == "50"
    protected = redact_sensitive(None, "info", {
        "internal_token": "synthetic-internal", "database_url": "synthetic-db",
        "credentials": "synthetic-credentials", "dsn": "synthetic-dsn",
    })
    assert set(protected.values()) == {"[REDACTED]"}
    assert "synthetic-pass" not in redact_text("postgresql+asyncpg://u:synthetic-pass@db/q")
