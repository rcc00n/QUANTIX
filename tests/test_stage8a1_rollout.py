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








