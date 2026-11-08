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




