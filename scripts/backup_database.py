#!/usr/bin/env python3
# ruff: noqa: UP017 -- Local host and audited Ubuntu22.04 use Python3.10.
"""Private consistent pg_dump archive. No passwords on argv or in output metadata."""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rotate_backups(directory: Path, keep: int):
    """Rotate only self-identified duplicate recovery archives, never application data."""
    archives = []
    for meta in directory.glob("quantix-*.dump.json"):
        archive = meta.with_suffix("")
        if not re.fullmatch(r"quantix-\d{8}T\d{6}Z\.dump", archive.name):
            continue
        if any(
            p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o077
            for p in (meta, archive)
        ):
            continue
        try:
            value = json.loads(meta.read_text())
            valid = (
                value.get("kind") == "PRIVATE_POSTGRESQL_BACKUP"
                and value.get("production") is True
                and value.get("archive") == archive.name
                and value.get("bytes") == archive.stat().st_size
                and re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
            )
        except (OSError, ValueError, TypeError):
            continue
        if valid:
            archives.append((archive, meta))
    for archive, meta in sorted(archives, key=lambda pair: pair[0].name, reverse=True)[
        keep:
    ]:
        archive.unlink()
        meta.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", type=int, default=2)
    args = parser.parse_args()
    if not 2 <= args.keep <= 30:
        raise SystemExit("Keep between2and30verified production recovery copies")
    compose = ["docker", "compose"]
    if args.production:
        compose += ["-f", "infra/compose.production.yaml"]
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / ".local/backups" / f"quantix-{timestamp}.dump"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists():
        raise SystemExit("Refusing to overwrite an existing backup")
    fd, temporary = tempfile.mkstemp(prefix=".quantix-backup-", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            process = subprocess.run(
                compose
                + [
                    "exec",
                    "-T",
                    "db",
                    "sh",
                    "-c",
                    (
                        "exec pg_dump -Fc --no-owner --no-privileges "
                        '-U "$POSTGRES_USER" -d "$POSTGRES_DB"'
                    ),
                ],
                cwd=ROOT,
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=600,
                check=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        if process.returncode:
            raise SystemExit("Database backup failed; no artifact published")
        digest = hashlib.sha256()
        with open(temporary, "rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise SystemExit("Invalid PostgreSQL custom-format archive")
            stream.seek(0)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        os.link(temporary, output)  # Exclusive publish, preserving0600 mode.
        metadata = {
            "kind": "PRIVATE_POSTGRESQL_BACKUP",
            "archive": output.name,
            "sha256": digest.hexdigest(),
            "bytes": output.stat().st_size,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "production": args.production,
            "restore_verified": False,
        }
        meta = output.with_suffix(output.suffix + ".json")
        with os.fdopen(
            os.open(meta, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
        ) as f:
            json.dump(metadata, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if args.production and args.output is None:
            rotate_backups(output.parent, args.keep)
        print(json.dumps(metadata))
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
