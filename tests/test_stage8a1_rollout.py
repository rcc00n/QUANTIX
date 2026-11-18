"""SYNTHETIC command/snapshot fixtures for the private rollout runner."""

import fcntl
import json
from argparse import Namespace
from contextlib import ExitStack

import pytest
from test_capacity_policy import load_script, preflight, replacement_inventory

rollout = load_script("rollout_stage8a1")


@pytest.fixture
def transaction(monkeypatch, tmp_path):
    inventory = replacement_inventory()
    target = inventory[1]
    target["State"].update(Status="running", Running=True, Pid=100)
    args = Namespace(
        mode="candidate",
        role="collector",
        release="stage8a1",
        current_container_id=target["Id"],
        current_image_id=target["Image"],
        replacement_image_id="sha256:" + "b" * 64,
    )
    calls = []

    def checked(cmd, **_kwargs):
        calls.append(cmd)
        if "stop" in cmd:
            target["State"].update(Status="exited", Running=False, Pid=0)
        return ""

    def restore(role, old, _previous, outcome):
        calls.append(["restored", role, old["id"]])
        outcome["state"] = "ORIGINAL_RESTORED"

    monkeypatch.setattr(rollout, "PRIVATE", tmp_path)
    monkeypatch.setattr(rollout, "containers", lambda: inventory)
    monkeypatch.setattr(rollout, "checked", checked)
    monkeypatch.setattr(rollout, "inspect_images", lambda *_args: None)
    monkeypatch.setattr(rollout, "sentinel", lambda *_args: "SYNTHETIC_STABLE")
    monkeypatch.setattr(rollout, "require_live_environment", lambda *_args: None)
    monkeypatch.setattr(rollout, "check_config", lambda *_args: ["SYNTHETIC_COMPOSE_COMMAND"])
    monkeypatch.setattr(rollout, "start_previous", restore)
    monkeypatch.setattr(rollout, "observe_running", lambda *_args, **_kw: {"status": "running"})
    outcome = {"state": "UNCHANGED"}
    return args, outcome, calls


def test_stops_only_exact_container_then_starts_one_role_if_gate_passes(monkeypatch, transaction):
    args, outcome, calls = transaction

    def evaluate(*_args):
        calls.append(["SYNTHETIC_POST_STOP_GATE"])
        return {"single_role_start_gate": True, "required_guard_gate_memavailable_mib": 1472}

    monkeypatch.setattr(preflight, "replacement_preflight", evaluate)
    rollout.replace(args, outcome)
    assert outcome["state"] == "SELECTED_ROLE_HEALTHY"
    assert calls[0] == [*preflight.LOCAL_DOCKER, "stop", "--time", "25", args.current_container_id]
    assert calls[1] == ["SYNTHETIC_POST_STOP_GATE"]
    assert calls[2][-1] == args.role
    assert "--no-deps" in calls[2]
    assert len(calls) == 3


@pytest.mark.parametrize("can_resume", [True, False])
def test_failed_gate_preserves_original_only_with_proven_restart_headroom(
    monkeypatch, transaction, can_resume
):
    args, outcome, calls = transaction
    monkeypatch.setattr(
        preflight,
        "replacement_preflight",
        lambda *_: {
            "single_role_start_gate": False,
            "gates": {
                "complete_post_stop_memory_window": True,
                "reserve_and_startup_preserved": can_resume,
            },
        },
    )
    with pytest.raises(preflight.CapacityProofError):
        rollout.replace(args, outcome)
    assert outcome["state"] == ("ORIGINAL_RESTORED" if can_resume else "ORIGINAL_STOPPED")
    assert all("up" not in call for call in calls)
    assert len(calls) == (2 if can_resume else 1)


def test_changed_old_instance_fails_before_any_stop(transaction):
    args, outcome, calls = transaction
    args.current_container_id = "c" * 64
    with pytest.raises(preflight.CapacityProofError, match="ORIGINAL_DEPLOYED_INSTANCE_CHANGED"):
        rollout.replace(args, outcome)
    assert outcome["state"] == "UNCHANGED"
    assert not calls


@pytest.mark.parametrize("role", ("backend", "collector", "frontend"))
@pytest.mark.parametrize("mode", ("candidate", "rollback"))
@pytest.mark.parametrize(
    ("release", "previous"),
    (
        ("stage8b1", "stage8a1"),
        ("stage8c", "stage8a1"),
        ("marathon", "stage8c"),
        ("paper-cohort", "marathon"),
    ),
)
def test_new_release_uses_exact_rollback_and_candidate_tags(
    monkeypatch, role, mode, release, previous
):
    project = "quantix-frontend" if role == "frontend" else "quantix-backend"
    old = f"{project}:0.7.1-{previous}"
    new = f"{project}:0.7.1-{release}"
    image_ids = {old: "sha256:" + "a" * 64, new: "sha256:" + "b" * 64}
    current, replacement = (old, new) if mode == "candidate" else (new, old)
    monkeypatch.setattr(preflight, "docker_data", lambda cmd: image_ids[cmd[-1]])
    assert (
        rollout.inspect_images(role, image_ids[current], image_ids[replacement], mode, release)
        == replacement
    )
    with pytest.raises(preflight.CapacityProofError, match="PINNED_IMAGE_TAG_CHANGED"):
        rollout.inspect_images(role, image_ids[replacement], image_ids[current], mode, release)


@pytest.mark.parametrize("release", ("stage8b1", "stage8c", "marathon", "paper-cohort"))
def test_new_release_cannot_restart_the_active_notification_consumer(transaction, release):
    args, outcome, calls = transaction
    args.release = release
    args.role = "notifications"
    with pytest.raises(preflight.CapacityProofError, match="ROLE_NOT_APPROVED_FOR_RELEASE"):
        rollout.replace(args, outcome)
    assert outcome["state"] == "UNCHANGED"
    assert not calls


def test_stage8b1_notification_pin_rejects_image_drift(monkeypatch):
    pinned = "sha256:" + "a" * 64
    snapshot = {"owned": {"notifications": {"image_id": pinned}}}
    monkeypatch.setattr(preflight, "docker_data", lambda _cmd: pinned)
    rollout.verify_notification_pin(snapshot)
    snapshot["owned"]["notifications"]["image_id"] = "sha256:" + "b" * 64
    with pytest.raises(preflight.CapacityProofError, match="UNCHANGED_NOTIFICATION_IMAGE_MISMATCH"):
        rollout.verify_notification_pin(snapshot)


@pytest.mark.parametrize("reply", ["compatible\n", "incompatible\n", ""])
def test_stage8c_paper_check_requires_a_positive_read_only_database_result(monkeypatch, reply):
    calls = []

    def checked(cmd, *, timeout):
        calls.append((cmd, timeout))
        return reply

    monkeypatch.setattr(rollout, "checked", checked)
    db_id = "b" * 64
    if reply == "compatible\n":
        rollout.verify_stage8c_paper_compatibility(db_id)
    else:
        with pytest.raises(preflight.CapacityProofError, match="STAGE8C_PAPER_COMPATIBILITY"):
            rollout.verify_stage8c_paper_compatibility(db_id)
    [(command, timeout)] = calls
    assert command[:5] == [*preflight.LOCAL_DOCKER, "exec", db_id]
    assert any("default_transaction_read_only=on" in arg for arg in command)
    assert timeout == 6


def test_full_export_worker_lock_blocks_maintenance_without_touching_archive(monkeypatch, tmp_path):
    mount = tmp_path / "export_volume"
    nested = mount / "quantix-full-exports"
    nested.mkdir(parents=True)
    archive = nested / "synthetic-completed-export.zip"
    archive.write_bytes(b"SYNTHETIC fixture content")
    backend = {
        "Id": "a" * 64,
        "Mounts": [
            {
                "Type": "volume",
                "Name": "quantix_export_temp",
                "Source": str(mount),
                "Destination": "/var/tmp/quantix-export",
            }
        ],
    }
    volume = {
        "Mountpoint": str(mount),
        "Labels": {"com.docker.compose.project": "quantix"},
    }

    def docker_data(command):
        if command[0] == "volume":
            return json.dumps([volume])
        assert command[:2] == ["exec", backend["Id"]]
        return ""

    monkeypatch.setattr(preflight, "docker_data", docker_data)
    with (nested / ".worker.lock").open("w") as busy, ExitStack() as cleanup:
        fcntl.flock(busy.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(preflight.CapacityProofError, match="FULL_EXPORT_IN_PROGRESS"):
            rollout.export_lock([backend], backend["Id"], cleanup)
    assert archive.read_bytes() == b"SYNTHETIC fixture content"
