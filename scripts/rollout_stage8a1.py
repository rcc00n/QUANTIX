#!/usr/bin/env python3
# ruff: noqa: UP017 -- The production host runs Python 3.10.
"""Controlled QUANTIX image replacement. Use only after the documented global gates."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

import production_preflight as gate

ROOT = gate.PRODUCTION_ROOT
PRIVATE = ROOT / ".local/stage8a1"
BASE = ROOT / "infra/compose.production.yaml"
COMPOSE = [*gate.LOCAL_DOCKER, "compose", "--project-name", "quantix", "-f", str(BASE)]
OVERLAYS = {
    "stage8a1": {
        "candidate": "compose.stage8a.candidate.yaml",
        "rollback": "compose.stage8a.rollback.yaml",
    },
    "stage8b1": {
        "candidate": "compose.stage8b1.candidate.yaml",
        "rollback": "compose.stage8b1.rollback.yaml",
    },
    "stage8c": {
        "candidate": "compose.stage8c.candidate.yaml",
        "rollback": "compose.stage8c.rollback.yaml",
    },
    "marathon": {
        "candidate": "compose.marathon.candidate.yaml",
        "rollback": "compose.marathon.rollback.yaml",
    },
    "paper-cohort": {
        "candidate": "compose.paper-cohort.candidate.yaml",
        "rollback": "compose.paper-cohort.rollback.yaml",
    },
}
EXPORT_MOUNT = "/var/tmp/quantix-export"
STAGE8C_PAPER_COMPATIBILITY = """
SELECT CASE WHEN
    EXISTS (SELECT 1 FROM paper_controls
            WHERE status = 'ACTIVE' AND settings ? 'strategy_lab_profile')
 OR EXISTS (SELECT 1 FROM paper_trades
            WHERE completed_at IS NULL AND (assumptions -> 'strategy') ? 'strategy_lab_profile')
 OR EXISTS (SELECT 1 FROM strategy_heads h JOIN strategy_versions v
            ON h.strategy_version_id = v.id WHERE v.settings ? 'strategy_lab_profile')
THEN 'incompatible' ELSE 'compatible' END;
"""


def checked(args, *, timeout=20):
    completed = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if completed.returncode:
        raise gate.CapacityProofError("COMMAND_FAILED")
    return completed.stdout


def inspect_images(role, current_id, replacement_id, mode, release="stage8a1"):
    previous, candidate = gate.replacement_image_transition(role, release)
    ids = {candidate: replacement_id, previous: current_id}
    if mode == "rollback":
        ids = {candidate: current_id, previous: replacement_id}
    for tag, expected in ids.items():
        if gate.docker_data(["image", "inspect", "--format", "{{.Id}}", tag]).strip() != expected:
            raise gate.CapacityProofError("PINNED_IMAGE_TAG_CHANGED")
    return candidate if mode == "candidate" else previous


def verify_notification_pin(snapshot):
    _, tag = gate.replacement_image_transition("notifications", "stage8a1")
    image = gate.docker_data(["image", "inspect", "--format", "{{.Id}}", tag]).strip()
    if snapshot["owned"]["notifications"]["image_id"] != image:
        raise gate.CapacityProofError("UNCHANGED_NOTIFICATION_IMAGE_MISMATCH")


def verify_stage8c_paper_compatibility(db_id):
    """Refuse to start a legacy paper parser on active newer strategy state."""
    result = checked(
        [
            *gate.LOCAL_DOCKER,
            "exec",
            db_id,
            "sh",
            "-c",
            'PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=2500" '
            'exec psql -X -Aqt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
            "quantix-stage8c-compatibility",
            STAGE8C_PAPER_COMPATIBILITY,
        ],
        timeout=6,
    ).strip()
    if result != "compatible":
        raise gate.CapacityProofError("STAGE8C_PAPER_COMPATIBILITY_NOT_PROVEN")


def release_path(release):
    return PRIVATE.parent / release


def check_config(mode, release="stage8a1"):
    """Validate both directions using Compose normalization, with no config output."""
    base = json.loads(checked([*COMPOSE, "config", "--format", "json"]))
    if base["name"] != "quantix" or set(base["services"]) != {*gate.REQUIRED_ROLES, "migrate"}:
        raise gate.CapacityProofError("UNEXPECTED_COMPOSE_PROJECT")
    for role, cap in gate.PRODUCTION_MEMORY_MIB.items():
        configured = base["services"][role]
        if int(configured["mem_limit"]) != cap * gate.MIB:
            raise gate.CapacityProofError("BASE_CAP_CHANGED")
        if int(configured["memswap_limit"]) != cap * gate.MIB:
            raise gate.CapacityProofError("BASE_SWAP_CHANGED")
    overlays = OVERLAYS[release]
    private = release_path(release)
    for name, filename in overlays.items():
        overlay = [*COMPOSE, "-f", str(private / filename)]
        config = json.loads(checked([*overlay, "config", "--format", "json"]))
        for role in gate.REPLACEABLE_ROLES:
            image = config["services"][role]["image"]
            if role in gate.REPLACEMENT_ROLES[release]:
                old, new = gate.replacement_image_transition(role, release)
                expected = new if name == "candidate" else old
            else:
                _, expected = gate.replacement_image_transition(role, "stage8a1")
            if image != expected:
                raise gate.CapacityProofError("UNEXPECTED_OVERLAY_IMAGE")
            config["services"][role]["image"] = base["services"][role]["image"]
        if config != base:
            raise gate.CapacityProofError("NON_IMAGE_COMPOSE_CHANGE")
    return [*COMPOSE, "-f", str(private / overlays[mode])]


def containers():
    ids = gate.docker_data(["ps", "-aq", "--no-trunc"]).split()
    if not ids or len(ids) > 128:
        raise gate.CapacityProofError("UNEXPECTED_CONTAINER_INVENTORY_SIZE")
    return json.loads(gate.docker_data(["inspect", "--type", "container", *ids]))


def nginx_sentinel():
    configuration = checked(["nginx", "-T"])
    service = checked(
        ["systemctl", "show", "--property=MainPID", "--property=NRestarts", "nginx.service"]
    )
    return hashlib.sha256(configuration.encode()).hexdigest(), service


def sentinel(inventory, role, snapshot):
    protected = {}
    for item in inventory:
        labels = item["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") == "quantix":
            continue
        host = dict(item["HostConfig"])
        if isinstance(host.get("Links"), list):
            host["Links"] = sorted(host["Links"])
        mounts = sorted(item.get("Mounts", []), key=lambda mount: mount["Destination"])
        protected[item["Id"]] = hashlib.sha256(
            gate.canonical([item["Image"], item["Config"], host, mounts])
        ).hexdigest()
    return (
        {key: item for key, item in snapshot["owned"].items() if key != role},
        snapshot["protected_running"],
        protected,
        gate.read_guard_identity(),
        nginx_sentinel(),
    )


def require_live_environment(required_mib):
    if checked(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip() != "yes":
        raise gate.CapacityProofError("VPS_CLOCK_NOT_SYNCHRONIZED")
    for address in ("https://badguymotors.com", "https://raccncode.com"):
        if gate.https_status(address) != 200:
            raise gate.CapacityProofError("PROTECTED_HTTPS_FAILED")
    for unit in ("quantix-backup.service", "quantix-tls-renew.service"):
        status = checked(["systemctl", "show", "--property=ActiveState", "--value", unit])
        if status.strip() != "inactive":
            raise gate.CapacityProofError("QUANTIX_MAINTENANCE_JOB_NOT_IDLE")
    if shutil.disk_usage(ROOT).free < gate.REPLACEMENT_DISK_RESERVE_BYTES:
        raise gate.CapacityProofError("QUANTIX_DISK_RESERVE_LOW")
    if gate.memory()["MemAvailable"] < 1024 * required_mib:
        raise gate.CapacityProofError("CURRENT_HEADROOM_TOO_LOW")


def export_lock(inventory, backend_id, stack):
    backend = next(item for item in inventory if item["Id"] == backend_id)
    mount = next(item for item in backend["Mounts"] if item["Destination"] == EXPORT_MOUNT)
    if mount["Type"] != "volume" or mount["Name"] != "quantix_export_temp":
        raise gate.CapacityProofError("UNEXPECTED_EXPORT_VOLUME")
    root = Path(mount["Source"]).resolve(strict=True)
    volume = json.loads(gate.docker_data(["volume", "inspect", "quantix_export_temp"]))[0]
    if (
        Path(volume["Mountpoint"]).resolve(strict=True) != root
        or volume["Labels"]["com.docker.compose.project"] != "quantix"
    ):
        raise gate.CapacityProofError("UNEXPECTED_EXPORT_VOLUME")
    # Use the service user to create a missing lock with correct ownership. The app
    # and runner both flock this same file. Completed exports are never opened.
    code = (
        "import os; from pathlib import Path; "
        "root=Path('/var/tmp/quantix-export/quantix-full-exports'); "
        "assert not root.is_symlink(); "
        "root.mkdir(mode=0o700, exist_ok=True); "
        "os.close(os.open(root/'.worker.lock', os.O_RDONLY|os.O_CREAT|os.O_NOFOLLOW, 0o600))"
    )
    gate.docker_data(["exec", backend_id, "python", "-c", code])
    path = root / "quantix-full-exports/.worker.lock"
    if path.parent.resolve(strict=True) != path.parent:
        raise gate.CapacityProofError("UNEXPECTED_EXPORT_LOCK_PATH")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    stack.callback(os.close, fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise gate.CapacityProofError("UNEXPECTED_EXPORT_LOCK_PATH")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise gate.CapacityProofError("FULL_EXPORT_IN_PROGRESS") from None
    if next(root.glob("quantix-export-*.zip"), None) is not None:
        raise gate.CapacityProofError("LEGACY_EXPORT_ARTIFACT_PRESENT")


def observe_running(role, expected_id, expected_image, previous, *, seconds):
    """Keep the same guard and allow only the selected runtime identity to change."""
    deadline = time.monotonic() + seconds
    while True:
        inventory = containers()
        snapshot = gate.replacement_snapshot(inventory, None, None)
        selected = snapshot["owned"][role]
        if selected["image_id"] != expected_image or (
            expected_id is not None and selected["id"] != expected_id
        ):
            raise gate.CapacityProofError("RUNNING_IMAGE_OR_CONTAINER_MISMATCH")
        if gate.memory()["MemAvailable"] < 1024 * gate.GUARD_SUSTAINED_FLOOR_MIB:
            raise gate.CapacityProofError("RUNNING_GUARD_HEADROOM_LOW")
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    if sentinel(inventory, role, snapshot) != previous:
        raise gate.CapacityProofError("PROTECTED_SENTINEL_CHANGED")
    require_live_environment(gate.GUARD_SUSTAINED_FLOOR_MIB)
    if gate.https_status("https://quantix.raccncode.com/login") != 200:
        raise gate.CapacityProofError("QUANTIX_PUBLIC_HTTPS_FAILED")
    return selected


def start_previous(role, old, previous, outcome, *, health_timeout=100):
    """Recover the known service only after the original reserve and safety checks."""
    inventory = containers()
    stopped = gate.replacement_snapshot(inventory, role, old["id"])
    if sentinel(inventory, role, stopped) != previous:
        raise gate.CapacityProofError("PROTECTED_SENTINEL_CHANGED")
    memory_floor = gate.PRODUCTION_MEMORY_MIB[role] + gate.RESERVE_MIB + gate.STARTUP_MARGIN_MIB
    require_live_environment(memory_floor)
    if stopped["owned"][role]["image_id"] != old["image_id"]:
        raise gate.CapacityProofError("ORIGINAL_CONTAINER_IMAGE_CHANGED")
    outcome["state"] = "STARTING_ORIGINAL"
    checked([*gate.LOCAL_DOCKER, "start", old["id"]])
    deadline = time.monotonic() + health_timeout
    while time.monotonic() < deadline:
        current = next(item for item in containers() if item["Id"] == old["id"])
        state = current["State"]
        if not state["Running"] or state["OOMKilled"]:
            raise gate.CapacityProofError("ORIGINAL_FAILED_TO_RECOVER")
        if state.get("Health", {}).get("Status") == "healthy":
            outcome["restored"] = observe_running(
                role, old["id"], old["image_id"], previous, seconds=10
            )
            outcome["state"] = "ORIGINAL_RESTORED"
            return
        time.sleep(2)
    raise gate.CapacityProofError("ORIGINAL_HEALTH_TIMEOUT")


def record_selected_instance(role, outcome):
    for item in containers():
        if item["Name"] == f"/quantix-{role}-1":
            outcome["selected_observed"] = {
                "id": item["Id"],
                "image_id": item["Image"],
                "status": item["State"]["Status"],
                "health": item["State"].get("Health", {}).get("Status"),
            }
            return
    outcome["selected_observed"] = None


def replace(args, outcome):
    if args.role not in gate.REPLACEMENT_ROLES[args.release]:
        raise gate.CapacityProofError("ROLE_NOT_APPROVED_FOR_RELEASE")
    with ExitStack() as stack:
        # Use the original rollout lock for both releases to prevent overlap.
        lock_fd = os.open(
            PRIVATE / "serial-rollout.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        stack.callback(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = check_config(args.mode, args.release)
        inspect_images(
            args.role, args.current_image_id, args.replacement_image_id, args.mode, args.release
        )
        inventory = containers()
        snapshot = gate.replacement_snapshot(
            inventory, None, None, unhealthy_role=args.role if args.mode == "rollback" else None
        )
        keep_notification_worker = "notifications" not in gate.REPLACEMENT_ROLES[args.release]
        if keep_notification_worker:
            verify_notification_pin(snapshot)
        if args.release == "marathon" and args.mode == "rollback":
            verify_stage8c_paper_compatibility(snapshot["owned"]["db"]["id"])
        old = snapshot["owned"][args.role]
        if old["id"] != args.current_container_id or old["image_id"] != args.current_image_id:
            raise gate.CapacityProofError("ORIGINAL_DEPLOYED_INSTANCE_CHANGED")
        previous = sentinel(inventory, args.role, snapshot)
        require_live_environment(gate.GUARD_SUSTAINED_FLOOR_MIB + gate.STARTUP_MARGIN_MIB)
        if args.role == "backend":
            export_lock(inventory, old["id"], stack)
        outcome["state"] = "STOPPING_ORIGINAL"
        checked([*gate.LOCAL_DOCKER, "stop", "--time", "25", old["id"]], timeout=35)
        outcome["state"] = "ORIGINAL_STOPPED"
        result = gate.replacement_preflight(
            args.role, old["id"], args.replacement_image_id, args.release
        )
        outcome["preflight"] = result
        try:
            if not result["single_role_start_gate"]:
                raise gate.CapacityProofError("POST_STOP_REPLACEMENT_GATE_DENIED")
            inventory = containers()
            stopped = gate.replacement_snapshot(inventory, args.role, old["id"])
            if sentinel(inventory, args.role, stopped) != previous:
                raise gate.CapacityProofError("PROTECTED_SENTINEL_CHANGED")
            if keep_notification_worker:
                verify_notification_pin(stopped)
            if args.release == "marathon" and args.mode == "rollback":
                verify_stage8c_paper_compatibility(stopped["owned"]["db"]["id"])
            inspect_images(
                args.role, args.current_image_id, args.replacement_image_id, args.mode, args.release
            )
            check_config(args.mode, args.release)
            require_live_environment(result["required_guard_gate_memavailable_mib"])
        except (gate.CapacityProofError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            start_gates = result.get("gates", {})
            if not (
                start_gates.get("complete_post_stop_memory_window")
                and start_gates.get("reserve_and_startup_preserved")
            ):
                raise gate.CapacityProofError("ORIGINAL_RESTART_HEADROOM_NOT_PROVEN") from None
            start_previous(args.role, old, previous, outcome)
            raise
        outcome["state"] = "REPLACING_ORIGINAL"
        checked(
            [
                *command,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "100",
                args.role,
            ],
            timeout=120,
        )
        outcome["selected"] = observe_running(
            args.role, None, args.replacement_image_id, previous, seconds=15
        )
        outcome["state"] = "SELECTED_ROLE_HEALTHY"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("candidate", "rollback"))
    parser.add_argument("role", choices=gate.REPLACEABLE_ROLES)
    parser.add_argument("--release", choices=OVERLAYS, default="stage8a1")
    parser.add_argument("--current-container-id", required=True)
    parser.add_argument("--current-image-id", required=True)
    parser.add_argument("--replacement-image-id", required=True)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[a-f0-9]{64}", args.current_container_id)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", args.current_image_id)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", args.replacement_image_id)
        or args.current_image_id == args.replacement_image_id
    ):
        parser.error("Supply distinct full image IDs and exact current container ID")
    outcome = {
        "mode": args.mode,
        "role": args.role,
        "release": args.release,
        "state": "UNCHANGED",
    }
    try:
        if os.geteuid() or Path(__file__).resolve().parent != release_path(args.release):
            raise gate.CapacityProofError("RUNNER_OUTSIDE_PRIVATE_QUANTIX_INSTALLATION")
        replace(args, outcome)
    except gate.CapacityProofError as error:
        outcome["error_code"] = str(error)
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        outcome["error_code"] = "ROLLOUT_INTERRUPTED_OR_MEASUREMENT_FAILED"
    if outcome["state"] in {"STOPPING_ORIGINAL", "ORIGINAL_STOPPED", "REPLACING_ORIGINAL"}:
        try:
            record_selected_instance(args.role, outcome)
        except (gate.CapacityProofError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            outcome["selected_observation_failed"] = True
    outcome["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["state"] == "SELECTED_ROLE_HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
