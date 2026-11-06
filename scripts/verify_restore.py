#!/usr/bin/env python3
"""Restore a private backup only in a new labelled, network-isolated disposable DB."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4


def call(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    if not args.archive.is_file() or args.archive.stat().st_mode & 0o077:
        raise SystemExit("Restore verification requires an existing private backup")
    name = "quantix-restore-check-" + uuid4().hex[:12]
    volume = name + "-data"
    created = False
    volume_created = False
    try:
        existing = subprocess.run(["docker", "volume", "inspect", volume],
                                  capture_output=True, check=False)
        if existing.returncode == 0:
            raise SystemExit("Refusing an existing restore verification volume")
        call(["docker", "volume", "create", "--label",
              "com.quantix.purpose=restore-verification", volume])
        volume_created = True
        call(["docker", "run", "-d", "--name", name, "--network", "none",
              "--label", "com.quantix.purpose=restore-verification",
              "--mount", f"type=volume,source={volume},target=/var/lib/postgresql/data",
              "--memory", "512m", "--cpus", "1",
              "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=restore_check",
              "postgres:16-alpine"])
        created = True
        for _ in range(60):
            check = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                                   capture_output=True, check=False)
            if check.returncode == 0:
                break
            time.sleep(1)
        else:
            raise SystemExit("Disposable restore database did not become ready")
        with args.archive.open("rb") as source:
            result = subprocess.run(
                ["docker", "exec", "-i", name, "pg_restore", "-U", "postgres",
                 "-d", "restore_check", "--no-owner", "--no-privileges", "--exit-on-error"],
                stdin=source, capture_output=True, timeout=600, check=False,
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
        output = call(["docker", "exec", name, "psql", "-U", "postgres", "-d",
                       "restore_check", "-XAtc", query], text=True).stdout.strip()
        proof = json.loads(output)
        if proof["unbalanced_journals"] != 0:
            raise SystemExit("Restored ledger failed balance reconciliation")
        digest = hashlib.sha256()
        with args.archive.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        proof.update(kind="ACTUAL_ISOLATED_RESTORE_VERIFICATION", network="none",
                     backup_sha256=digest.hexdigest(),
                     application_database_mutations=0, restored=True,
                     storage="exclusive_temporary_disk_volume", memory_limit_mib=512)
        target = args.archive.with_suffix(args.archive.suffix + ".restore.json")
        import os
        with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
            json.dump(proof, f, indent=2)
        print(json.dumps(proof))
    finally:
        # The exact random ID was created above. Never prune or target another service.
        if created:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        if volume_created:
            ownership = subprocess.run(
                ["docker", "volume", "inspect", "--format",
                 '{{index .Labels "com.quantix.purpose"}}', volume],
                capture_output=True, text=True, check=False,
            )
            if ownership.returncode == 0 and ownership.stdout.strip() == "restore-verification":
                subprocess.run(["docker", "volume", "rm", volume],
                               capture_output=True, check=False)


if __name__ == "__main__":
    main()
