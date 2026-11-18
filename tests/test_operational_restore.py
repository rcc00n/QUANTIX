"""Fault injection for the isolated restore CLI, using synthetic bytes only."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

scripts = Path(__file__).resolve().parents[1] / "scripts"
if not scripts.is_dir():
    scripts = Path("/app/scripts")
spec = importlib.util.spec_from_file_location(
    "verify_restore", scripts / "verify_restore.py"
)
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)

SYNTHETIC_ARCHIVE = b"PGDMP_SYNTHETIC_RESTORE_FIXTURE"
CHECKSUM = hashlib.sha256(SYNTHETIC_ARCHIVE).hexdigest()
TOKEN = "a" * 32
NAME = "quantix-restore-check-" + TOKEN[:12]
VOLUME = NAME + "-data"
CONTAINER = "b" * 64


class SyntheticDocker:
    def __init__(self, *, failed=False, unbalanced=0):
        self.failed = failed
        self.unbalanced = unbalanced
        self.calls = []

    def run(self, cmd, **kwargs):
        assert cmd[0] == "docker"
        self.calls.append(cmd)
        commands = cmd[1:3]
        if commands == ["volume", "inspect"]:
            assert cmd[-1] == VOLUME
            return SimpleNamespace(returncode=1)
        if commands == ["volume", "create"]:
            assert cmd[-1] == VOLUME and f"{restore.LABEL}={TOKEN}" in cmd
        elif cmd[1] == "run":
            assert cmd[cmd.index("--network") + 1] == "none"
            assert cmd[cmd.index("--memory") + 1] == "512m"
            assert cmd[cmd.index("--name") + 1] == NAME
            assert f"{restore.LABEL}={TOKEN}" in cmd
        elif "pg_isready" in cmd:
            assert "127.0.0.1" in cmd
        elif "pg_restore" in cmd:
            assert (
                kwargs["stdin"].read() == SYNTHETIC_ARCHIVE and "--exit-on-error" in cmd
            )
            return SimpleNamespace(returncode=1 if self.failed else 0)
        elif "psql" in cmd:
            assert "unbalanced_journals" in cmd[-1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"actual_trades": 0, "unbalanced_journals": self.unbalanced}
                ),
            )
        elif commands == ["container", "ls"]:
            assert f"name=^/{NAME}$" in cmd and f"label={restore.LABEL}={TOKEN}" in cmd
            return SimpleNamespace(returncode=0, stdout=CONTAINER)
        elif commands == ["volume", "ls"]:
            assert f"name=^{VOLUME}$" in cmd and f"label={restore.LABEL}={TOKEN}" in cmd
            return SimpleNamespace(returncode=0, stdout=VOLUME)
        else:
            assert cmd in [
                ["docker", "rm", "-f", CONTAINER],
                ["docker", "volume", "rm", VOLUME],
            ]
        return SimpleNamespace(returncode=0)


def setup(tmp_path, monkeypatch, docker, checksum=CHECKSUM):
    archive = tmp_path / "synthetic-recovery.dump"
    archive.write_bytes(SYNTHETIC_ARCHIVE)
    archive.chmod(0o600)
    proof = tmp_path / "synthetic-restore-proof.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_restore.py",
            str(archive),
            "--expected-sha256",
            checksum,
            "--proof-file",
            str(proof),
            "--require-paper-only",
        ],
    )
    monkeypatch.setattr(restore, "uuid4", lambda: SimpleNamespace(hex=TOKEN))
    monkeypatch.setattr(restore.subprocess, "run", docker.run)
    return proof


def test_bad_checksum_aborts_before_docker_or_proof(tmp_path, monkeypatch):
    docker = SyntheticDocker()
    proof = setup(tmp_path, monkeypatch, docker, checksum="0" * 64)
    with pytest.raises(SystemExit, match="differs from the trusted source"):
        restore.main()
    assert docker.calls == [] and not proof.exists()


def test_verified_restore_publishes_private_immutable_proof_after_cleanup(
    tmp_path, monkeypatch
):
    docker = SyntheticDocker()
    proof = setup(tmp_path, monkeypatch, docker)
    restore.main()
    assert docker.calls[-1] == ["docker", "volume", "rm", VOLUME]
    payload = json.loads(proof.read_bytes())
    assert payload["restored"] and payload["trusted_sha256_matched"]
    assert payload["backup_sha256"] == CHECKSUM and payload["backup_bytes"] == len(
        SYNTHETIC_ARCHIVE
    )
    assert proof.stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob(".quantix-restore-proof-*"))
    old = len(docker.calls)
    with pytest.raises(SystemExit, match="proof already exists"):
        restore.main()
    assert len(docker.calls) == old
    assert json.loads(proof.read_bytes()) == payload


@pytest.mark.parametrize(
    "failed,unbalanced,reason",
    [(True, 0, "Isolated restore failed"), (False, 1, "failed balance reconciliation")],
)
def test_restore_failure_cleans_only_labeled_scratch_and_has_no_proof(
    tmp_path, monkeypatch, failed, unbalanced, reason
):
    docker = SyntheticDocker(failed=failed, unbalanced=unbalanced)
    proof = setup(tmp_path, monkeypatch, docker)
    with pytest.raises(SystemExit, match=reason):
        restore.main()
    assert ["docker", "rm", "-f", CONTAINER] in docker.calls
    assert docker.calls[-1] == ["docker", "volume", "rm", VOLUME]
    assert not proof.exists()
