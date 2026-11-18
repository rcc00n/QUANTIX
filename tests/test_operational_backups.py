"""Synthetic private backup files; no application or production data is deleted."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

scripts = Path(__file__).resolve().parents[1] / "scripts"
if not scripts.is_dir():
    scripts = Path("/app/scripts")
spec = importlib.util.spec_from_file_location(
    "backup_database", scripts / "backup_database.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
ARCHIVE = b"PGDMP_SYNTHETIC_DURABILITY_FIXTURE"


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
    events = []
    original_fsync, original_link = os.fsync, os.link

    def fake_dump(command, **kwargs):
        kwargs["stdout"].write(ARCHIVE)
        return SimpleNamespace(returncode=0)

    def fsync(fd):
        events.append(("fsync", Path(os.readlink(f"/proc/self/fd/{fd}")).name))
        return original_fsync(fd)

    def publish(source, target):
        events.append(("publish", Path(target).name))
        return original_link(source, target)

    target = tmp_path / "synthetic-recovery.dump"
    monkeypatch.setattr(sys, "argv", ["backup_database.py", "--output", str(target)])
    monkeypatch.setattr(backup.subprocess, "run", fake_dump)
    monkeypatch.setattr(backup.os, "fsync", fsync)
    monkeypatch.setattr(backup.os, "link", publish)
    backup.main()
    assert events[:2] == [("fsync", "archive"), ("fsync", "manifest")]
    assert events[-4:] == [
        ("publish", target.name),
        ("fsync", tmp_path.name),
        ("publish", backup.sidecar(target).name),
        ("fsync", tmp_path.name),
    ]
    assert target.read_bytes() == ARCHIVE
    assert target.stat().st_mode & 0o077 == 0
    assert json.loads(capsys.readouterr().out)["restore_verified"] is False


def synthetic_dump(command, **kwargs):
    assert command[:2] == ["docker", "compose"]
    kwargs["stdout"].write(ARCHIVE)
    return SimpleNamespace(returncode=0)


def test_interruption_after_archive_recovers_same_inode_without_dumping_again(
    tmp_path, monkeypatch
):
    target = tmp_path / "synthetic-recovery.dump"
    monkeypatch.setattr(sys, "argv", ["backup_database.py", "--output", str(target)])
    monkeypatch.setattr(backup.subprocess, "run", synthetic_dump)
    publish = backup.publish_link

    def interrupt_after_archive(source, destination):
        if destination == backup.sidecar(target):
            raise KeyboardInterrupt
        publish(source, destination)

    monkeypatch.setattr(backup, "publish_link", interrupt_after_archive)
    with pytest.raises(KeyboardInterrupt):
        backup.main()
    assert not backup.sidecar(target).exists()
    before = target.stat().st_ino
    assert len(list(tmp_path.glob(".quantix-backup-*"))) == 1
    monkeypatch.setattr(backup, "publish_link", publish)
    monkeypatch.setattr(
        backup.subprocess, "run", lambda *a, **kw: pytest.fail("Recovery reran pg_dump")
    )
    backup.main()
    backup.main()
    assert not list(tmp_path.glob(".quantix-backup-*"))
    assert target.stat().st_ino == before
    assert backup.verified_backup(target)["bytes"] == len(ARCHIVE)


def test_interrupted_dump_and_stale_private_scratch_leave_foreign_files(
    tmp_path, monkeypatch
):
    target = tmp_path / "synthetic-recovery.dump"
    stage = tmp_path / (".quantix-backup-" + "a" * 32)
    stage.mkdir(mode=0o700)
    (stage / "archive").write_bytes(b"INCOMPLETE")
    (stage / "archive").chmod(0o600)
    foreign = tmp_path / (".quantix-backup-" + "b" * 32)
    foreign.mkdir(mode=0o700)
    (foreign / "SYNTHETIC_OPERATOR_RECORD").write_bytes(b"must remain")

    def interrupt_dump(command, **kwargs):
        kwargs["stdout"].write(b"PGDMP_partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["backup_database.py", "--output", str(target)])
    monkeypatch.setattr(backup.subprocess, "run", interrupt_dump)
    with pytest.raises(KeyboardInterrupt):
        backup.main()
    assert list(tmp_path.glob(".quantix-backup-*")) == [foreign]
    assert not target.exists() and not backup.sidecar(target).exists()


def test_verified_copy_is_idempotent_with_conflicts_and_corruption(
    tmp_path, monkeypatch
):
    target = tmp_path / "synthetic-source.dump"
    directory = tmp_path / "synthetic-private-destination"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(sys, "argv", ["backup_database.py", "--output", str(target)])
    monkeypatch.setattr(backup.subprocess, "run", synthetic_dump)
    backup.main()
    source_data = backup.verified_backup(target)
    copy = directory / target.name
    assert backup.replicate(target, directory) == source_data
    before = copy.stat().st_ino
    assert backup.replicate(target, directory) == source_data
    assert backup.verified_backup(copy) == source_data
    assert copy.stat().st_ino == before
    assert copy.stat().st_mode & 0o077 == 0
    copy.write_bytes(ARCHIVE[:-1] + b"X")
    with pytest.raises(ValueError, match="checksum verification failed"):
        backup.replicate(target, directory)
    assert copy.read_bytes() == ARCHIVE[:-1] + b"X"
    sidecar_symlink = backup.sidecar(target)
    saved_manifest = sidecar_symlink.read_bytes()
    sidecar_symlink.unlink()
    sidecar_symlink.symlink_to(tmp_path / "synthetic-nonexistent")
    with pytest.raises(ValueError, match="private archive and bounded metadata"):
        backup.main()
    assert sidecar_symlink.is_symlink() and not list(tmp_path.glob(".quantix-backup-*"))
    assert json.loads(saved_manifest)["sha256"] == source_data["sha256"]


def test_directory_lock_prevents_overlapping_dump(tmp_path, monkeypatch):
    target = tmp_path / "synthetic-locked.dump"
    monkeypatch.setattr(sys, "argv", ["backup_database.py", "--output", str(target)])
    monkeypatch.setattr(
        backup.subprocess, "run", lambda *a, **kw: pytest.fail("Lock ignored")
    )
    with (
        backup.backup_directory(tmp_path),
        pytest.raises(ValueError, match="owns this directory"),
    ):
        backup.main()
    assert not target.exists()


@pytest.mark.parametrize("returncode,payload", [(1, ARCHIVE), (0, b"not a pg dump")])
def test_failed_or_invalid_dump_never_publishes(
    tmp_path, monkeypatch, returncode, payload
):
    target = tmp_path / "synthetic-invalid.dump"
    monkeypatch.setattr(sys, "argv", ["backup_database.py", "--output", str(target)])

    def fake_dump(command, **kwargs):
        kwargs["stdout"].write(payload)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(backup.subprocess, "run", fake_dump)
    with pytest.raises(
        ValueError, match="failed before publication|Invalid PostgreSQL"
    ):
        backup.main()
    assert not target.exists() and not backup.sidecar(target).exists()
    assert not list(tmp_path.glob(".quantix-backup-*"))
