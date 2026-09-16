"""Temporary SYNTHETIC credential files only; never rotate the real owner in tests."""

import importlib
from pathlib import Path

from arbiq.auth.security import verify_password


def test_reset_generates_memorable_protected_credential_without_logging(
    monkeypatch, tmp_path, capsys
):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if not scripts.exists():
        scripts = Path("/app/scripts")
    monkeypatch.syspath_prepend(str(scripts))
    reset = importlib.import_module("reset_password")
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "ADMIN_USERNAME=synthetic-owner\nADMIN_PASSWORD_HASH=synthetic-invalid\n"
    )
    reset.reset()
    password = (tmp_path / ".secrets/admin-password").read_text().strip()
    text = (tmp_path / ".env").read_text()
    encoded = next(
        line.split("=", 1)[1].strip("'")
        for line in text.splitlines()
        if line.startswith("ADMIN_PASSWORD_HASH=")
    )
    assert len(password) >= 16 and len(password.split("-")) == 7
    assert verify_password(password, encoded)
    assert password not in capsys.readouterr().out
    assert (
        "ADMIN_USERNAME=quantix" in text
        and "ADMIN_PREVIOUS_USERNAME=synthetic-owner" in text
    )
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".secrets/admin-password").stat().st_mode & 0o777 == 0o600
