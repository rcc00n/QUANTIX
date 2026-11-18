#!/usr/bin/env python3
# ruff: noqa: UP017 -- Local host and audited Ubuntu22.04 use Python3.10.
"""Restore a private backup only in a new labelled, network-isolated disposable DB."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

LABEL = "com.quantix.restore.run"
PURPOSE = "com.quantix.purpose=restore-verification"


def call(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=30, **kwargs)


def archive_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup(name, volume, token):
    """Remove only resources carrying the exact invocation's ownership label."""
    ownership = f"label={LABEL}={token}"
    identifier = call(
        [
            "docker",
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"name=^/{name}$",
            "--filter",
            ownership,
            "--format",
            "{{.ID}}",
        ],
        text=True,
    ).stdout.strip()
    if identifier:
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("Unexpected disposable container identity")
        call(["docker", "rm", "-f", identifier])
    storage = call(
        [
            "docker",
            "volume",
            "ls",
            "--filter",
            f"name=^{volume}$",
            "--filter",
            ownership,
            "--format",
            "{{.Name}}",
        ],
        text=True,
    ).stdout.strip()
    if storage == volume:
        call(["docker", "volume", "rm", volume])
    elif storage:
        raise ValueError("Unexpected disposable volume identity")


def publish_proof(target, proof):
    fd, tmp = tempfile.mkstemp(prefix=".quantix-restore-proof-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(proof, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--proof-file", type=Path)
    parser.add_argument("--require-paper-only", action="store_true")
    args = parser.parse_args()
    if (
        args.archive.is_symlink()
        or not args.archive.is_file()
        or args.archive.stat().st_mode & 0o077
    ):
        raise SystemExit("Restore verification requires an existing private backup")
    target = args.proof_file or args.archive.with_suffix(
        args.archive.suffix + ".restore.json"
    )
    if target.exists() or target.is_symlink():
        raise SystemExit("Restore proof already exists; supply a fresh proof path")
    expected = args.expected_sha256
    if expected is not None and (
        len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)
    ):
        raise SystemExit("Expected backup SHA-256 must be lowercase hexadecimal")
    checksum = archive_hash(args.archive)
    if expected is not None and expected != checksum:
        raise SystemExit("Backup SHA-256 differs from the trusted source")
    token = uuid4().hex
    name = "quantix-restore-check-" + token[:12]
    volume = name + "-data"
    ownership = f"{LABEL}={token}"
    existing = subprocess.run(
        ["docker", "volume", "inspect", volume],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if existing.returncode == 0:
        raise SystemExit("Refusing an existing restore verification volume")
    try:
        call(
            [
                "docker",
                "volume",
                "create",
                "--label",
                PURPOSE,
                "--label",
                ownership,
                volume,
            ]
        )
        call(
            [
                "docker",
                "run",
                "-d",
                "--pull",
                "never",
                "--name",
                name,
                "--network",
                "none",
                "--label",
                PURPOSE,
                "--label",
                ownership,
                "--mount",
                f"type=volume,source={volume},target=/var/lib/postgresql/data",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_DB=restore_check",
                "postgres:16-alpine",
            ]
        )
        for _ in range(60):
            check = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                ],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if check.returncode == 0:
                break
            time.sleep(1)
        else:
            raise SystemExit("Disposable restore database did not become ready")
        with args.archive.open("rb") as source:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "pg_restore",
                    "-U",
                    "postgres",
                    "-d",
                    "restore_check",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                ],
                stdin=source,
                capture_output=True,
                timeout=600,
                check=False,
            )
        if result.returncode:
            raise SystemExit("Isolated restore failed; original DB remains unchanged")
        query = """
SELECT json_build_object(
  'schema',(SELECT version_num FROM alembic_version),
  'sessions',(SELECT count(*) FROM paper_sessions),
  'paper_fills',(SELECT count(*) FROM paper_fills),
  'actual_trades',(SELECT count(*) FROM actual_trades),
  'ledger_entries',(SELECT count(*) FROM ledger_entries),
  'unbalanced_journals',(SELECT count(*) FROM (
    SELECT le.journal_id,va.asset_id FROM ledger_entries le
    JOIN virtual_accounts va ON va.id=le.account_id
    GROUP BY le.journal_id,va.asset_id HAVING sum(le.quantity)<>0
  ) violations)
);"""
        output = call(
            [
                "docker",
                "exec",
                name,
                "psql",
                "-U",
                "postgres",
                "-d",
                "restore_check",
                "-XAtc",
                query,
            ],
            text=True,
        ).stdout.strip()
        proof = json.loads(output)
        if proof["unbalanced_journals"] != 0:
            raise SystemExit("Restored ledger failed balance reconciliation")
        if args.require_paper_only and proof["actual_trades"]:
            raise SystemExit("Restored PAPER ONLY backup contains actual trade records")
        if archive_hash(args.archive) != checksum:
            raise SystemExit("The backup changed during restore verification")
        proof.update(
            kind="ACTUAL_ISOLATED_RESTORE_VERIFICATION",
            network="none",
            backup_sha256=checksum,
            trusted_sha256_matched=True if expected else None,
            backup_bytes=args.archive.stat().st_size,
            checked_at=datetime.now(timezone.utc).isoformat(),
            application_database_mutations=0,
            restored=True,
            storage="exclusive_temporary_disk_volume",
            memory_limit_mib=512,
        )
    finally:
        cleanup(name, volume, token)
    publish_proof(target, proof)
    print(json.dumps(proof))


def interrupted(signum, _frame):
    raise SystemExit(f"Restore verification interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise SystemExit(
            f"Restore verification could not finish ({type(exc).__name__})"
        ) from None
