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
}
EXPORT_MOUNT = "/var/tmp/quantix-export"


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


