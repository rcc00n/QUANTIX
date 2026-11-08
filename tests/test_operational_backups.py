"""Synthetic private backup files; no application or production data is deleted."""

import importlib.util
import json
from pathlib import Path

scripts = Path(__file__).resolve().parents[1] / "scripts"
if not scripts.is_dir():
    scripts = Path("/app/scripts")
spec = importlib.util.spec_from_file_location(
    "backup_database", scripts / "backup_database.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def create(directory, day, verified=True):
    archive = directory / f"quantix-202611{day:02}T000000Z.dump"
    archive.write_bytes(b"SYNTHETIC_PRIVATE_RECOVERY_ARCHIVE")
    archive.chmod(0o600)
    meta = archive.with_suffix(".dump.json")
    meta.write_text(
        json.dumps(
            {
                "kind": "PRIVATE_POSTGRESQL_BACKUP" if verified else "UNRELATED",
                "production": True,
                "archive": archive.name,
                "bytes": archive.stat().st_size,
                "sha256": "a" * 64,
            }
        )
    )
    meta.chmod(0o600)
    return archive


def test_rotation_preserves_newest_and_unknown_files(tmp_path):
    files = [create(tmp_path, day) for day in range(1, 5)]
    unknown = create(tmp_path, 5, False)
    backup.rotate_backups(tmp_path, keep=2)
    assert [file.exists() for file in files] == [False, False, True, True]
    assert unknown.exists()


def test_rotation_never_follows_symlinks(tmp_path):
    external = tmp_path / "SYNTHETIC_ledger.dump"
    external.write_text("SYNTHETIC_UNRELATED_DATA")
    link = tmp_path / "quantix-20261101T000000Z.dump"
    link.symlink_to(external)
    for day in range(2, 5):
        create(tmp_path, day)
    backup.rotate_backups(tmp_path, keep=2)
    assert external.read_text() == "SYNTHETIC_UNRELATED_DATA"
    assert link.is_symlink()


def test_backup_flushes_data_before_exclusive_publication(
    tmp_path, monkeypatch, capsys
):
    """SYNTHETIC pg_dump byte stream verifies durable publication, no database call."""
    import os
    import sys
    from types import SimpleNamespace

    events = []
    original_fsync, original_link = os.fsync, os.link

    def fake_dump(command, **kwargs):
        kwargs["stdout"].write(b"PGDMP_SYNTHETIC_DURABILITY_FIXTURE")
        return SimpleNamespace(returncode=0)

    def fsync(fd):
        events.append("fsync")
        return original_fsync(fd)

    def publish(source, target):
        events.append("publish")
        return original_link(source, target)

    target = tmp_path / "synthetic-recovery.dump"
    monkeypatch.setattr(sys, "argv", ["backup_database.py", "--output", str(target)])
    monkeypatch.setattr(backup.subprocess, "run", fake_dump)
    monkeypatch.setattr(backup.os, "fsync", fsync)
    monkeypatch.setattr(backup.os, "link", publish)
    backup.main()
    assert events[0:2] == ["fsync", "publish"]
    assert events[-2:] == ["fsync", "fsync"]
    assert target.read_bytes() == b"PGDMP_SYNTHETIC_DURABILITY_FIXTURE"
    assert target.stat().st_mode & 0o077 == 0
    assert json.loads(capsys.readouterr().out)["restore_verified"] is False
