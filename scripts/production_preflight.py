#!/usr/bin/env python3
# ruff: noqa: UP017 -- The audited Ubuntu 22.04 VPS uses Python 3.10.
"""Read-only VPS gate for cold deployment or one already-stopped image replacement.

Run over pinned SSH stdin. Optional cold-deployment proofs contain only validated
resource metrics and artifact hashes. The replacement gate measures actual Linux
availability after stop and returns a result for one service.
"""

import argparse
import base64
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext

DEFAULT_PLANNING_MIB = 1536
RESERVE_MIB = 512
STARTUP_MARGIN_MIB = 64
MIN_AFTER_SECONDS = 900
MIN_PEAK_SAVING_MIB = 200
BUDGET_QUANTUM_MIB = 64
PEAK_FIXED_MARGIN_MIB = 128
PEAK_FACTOR = Decimal("1.10")
MAX_PROOF_BYTES = 16384
MAX_PROOF_BASE64_BYTES = 4 * ((MAX_PROOF_BYTES + 2) // 3)
SCHEMA = "quantix-capacity-proof-v1"
REQUIRED_ROLES = ("backend", "collector", "db", "frontend", "notifications")
MIB = 1024**2
PRODUCTION_MEMORY_MIB = {
    "backend": 384,
    "collector": 768,
    "db": 320,
    "frontend": 192,
    "notifications": 160,
}
REPLACEABLE_ROLES = ("backend", "collector", "frontend", "notifications")
REPLACEMENT_ROLES = {
    "stage8a1": REPLACEABLE_ROLES,
    "stage8b1": ("backend", "collector", "frontend"),
    "stage8c": ("backend", "collector", "frontend"),
    "marathon": ("backend", "collector", "frontend"),
}
GUARD_SUSTAINED_FLOOR_MIB = 640
REPLACEMENT_DISK_RESERVE_BYTES = 6 * 1024**3
PRODUCTION_ROOT = pathlib.Path("/opt/quantix")
LOCAL_DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
REQUIRED_READ_CATEGORIES = (
    "analytics",
    "opportunities_custom",
    "opportunities_default",
    "overview",
)


class CapacityProofError(ValueError):
    """Safe code-only error; never echo raw proof or profile contents."""


def decimal_value(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise CapacityProofError("INVALID_NUMERIC_METRIC")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise CapacityProofError("INVALID_NUMERIC_METRIC") from None
    if (
        not number.is_finite()
        or number < 0
        or number > Decimal(1000000)
        or len(str(value)) > 128
        or number.as_tuple().exponent < -32
    ):
        raise CapacityProofError("INVALID_NUMERIC_METRIC")
    return number


def exact_int(value, *, minimum=0, maximum=100000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CapacityProofError("INVALID_INTEGER_METRIC")
    return value


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise CapacityProofError("INVALID_PROOF_STRUCTURE") from None


def seal_proof(value):
    result = dict(value)
    result.pop("proof_sha256", None)
    result["proof_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def bounded_json(data, maximum):
    if not isinstance(data, bytes) or len(data) > maximum:
        raise CapacityProofError("EVIDENCE_TOO_LARGE")

    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise CapacityProofError("DUPLICATE_JSON_KEY")
            result[key] = item
        return result

    def nonfinite(_value):
        raise CapacityProofError("NONFINITE_JSON_NUMBER")

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=nonfinite,
            object_pairs_hook=unique_pairs,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise CapacityProofError("INVALID_JSON_EVIDENCE") from None
    if not isinstance(value, dict):
        raise CapacityProofError("INVALID_PROOF_STRUCTURE")
    return value


def planning_envelope(after_peak_mib):
    peak = decimal_value(after_peak_mib)
    if peak <= 0:
        raise CapacityProofError("EMPTY_PEAK_MEASUREMENT")
    with localcontext() as context:
        context.prec = 80
        bound = max(peak * PEAK_FACTOR, peak + PEAK_FIXED_MARGIN_MIB)
        return (
            int((bound / BUDGET_QUANTUM_MIB).to_integral_value(rounding=ROUND_CEILING))
            * BUDGET_QUANTUM_MIB
        )


def validate_summary(value, *, after):
    expected = {
        "elapsed_seconds",
        "sample_count",
        "sample_first_seconds",
        "sample_last_seconds",
        "maximum_sample_gap_seconds",
        "stack_peak_mib",
        "service_peak_mib",
        "read_count",
        "read_status_codes",
        "read_categories",
        "coverage_sample_count",
        "minimum_coverage",
        "export_exit_code",
        "external_exchange_requests_forced",
        "stable_service_instances",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CapacityProofError("INVALID_PROFILE_SUMMARY")
    elapsed = decimal_value(value["elapsed_seconds"])
    first = decimal_value(value["sample_first_seconds"])
    last = decimal_value(value["sample_last_seconds"])
    gap = decimal_value(value["maximum_sample_gap_seconds"])
    exact_int(value["sample_count"], minimum=2, maximum=10000)
    if (
        elapsed <= 0
        or not 0 <= first < last <= elapsed
        or first > 30
        or elapsed - last > 30
        or gap > 30
    ):
        raise CapacityProofError("INCOMPLETE_OBSERVATION_WINDOW")
    if after and elapsed < MIN_AFTER_SECONDS:
        raise CapacityProofError("AFTER_PROFILE_SHORTER_THAN_900_SECONDS")
    if value["stable_service_instances"] is not True:
        raise CapacityProofError("SERVICE_RESTART_DURING_MEASUREMENT")
    peaks = value["service_peak_mib"]
    if not isinstance(peaks, dict) or set(peaks) != set(REQUIRED_ROLES):
        raise CapacityProofError("INCOMPLETE_SERVICE_MEASUREMENT")
    numbers = [decimal_value(peaks[role]) for role in REQUIRED_ROLES]
    peak = decimal_value(value["stack_peak_mib"])
    with localcontext() as context:
        context.prec = 80
        separate_peak_sum = sum(numbers, Decimal(0))
    if any(number <= 0 for number in numbers) or not max(numbers) <= peak <= separate_peak_sum:
        raise CapacityProofError("INCONSISTENT_PEAK_MEASUREMENT")
    exact_int(value["read_count"], minimum=len(REQUIRED_READ_CATEGORIES), maximum=10000)
    if value["read_status_codes"] != [200] or any(
        type(x) is not int for x in value["read_status_codes"]
    ):
        raise CapacityProofError("NORMAL_READ_FAILED")
    categories = value["read_categories"]
    if not isinstance(categories, list) or set(categories) != set(REQUIRED_READ_CATEGORIES):
        raise CapacityProofError("INCOMPLETE_READ_WORKLOAD")
    if type(value["export_exit_code"]) is not int or value["export_exit_code"] != 0:
        raise CapacityProofError("GENUINE_EXPORT_FAILED_OR_MISSING")
    if (
        type(value["external_exchange_requests_forced"]) is not int
        or value["external_exchange_requests_forced"] != 0
    ):
        raise CapacityProofError("FORCED_EXTERNAL_WORKLOAD")
    exact_int(value["coverage_sample_count"], minimum=1, maximum=10000)
    coverage = value["minimum_coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {
        "adapters",
        "venue_groups",
        "books",
    }:
        raise CapacityProofError("INCOMPLETE_COVERAGE_EVIDENCE")
    for key, minimum in (("adapters", 20), ("venue_groups", 19), ("books", 200)):
        if exact_int(coverage[key], maximum=100000) < minimum:
            raise CapacityProofError("MARKET_COVERAGE_REDUCED")
    return peak


def validate_capacity_proof(proof):
    expected = {
        "schema_version",
        "kind",
        "before",
        "after",
        "evidence_sha256",
        "planning_envelope_mib",
        "reserve_mib",
        "startup_margin_mib",
        "minimum_after_seconds",
        "minimum_peak_saving_mib",
        "peak_saving_mib",
        "cold_cache_savings_proven",
        "sustained_vps_coexistence_proven",
        "proof_sha256",
    }
    if not isinstance(proof, dict) or set(proof) != expected or proof["schema_version"] != SCHEMA:
        raise CapacityProofError("INVALID_PROOF_STRUCTURE")
    if proof["kind"] != "DERIVED_ACTUAL_LOCAL_CAPACITY_EVIDENCE":
        raise CapacityProofError("UNSUPPORTED_CAPACITY_EVIDENCE")
    if len(canonical(proof)) > MAX_PROOF_BYTES:
        raise CapacityProofError("EVIDENCE_TOO_LARGE")
    if proof.get("proof_sha256") != seal_proof(proof)["proof_sha256"]:
        raise CapacityProofError("PROOF_DIGEST_MISMATCH")
    for key, expected_value in (
        ("reserve_mib", RESERVE_MIB),
        ("startup_margin_mib", STARTUP_MARGIN_MIB),
        ("minimum_after_seconds", MIN_AFTER_SECONDS),
        ("minimum_peak_saving_mib", MIN_PEAK_SAVING_MIB),
    ):
        if type(proof[key]) is not int or proof[key] != expected_value:
            raise CapacityProofError("SAFETY_POLICY_CHANGED")
    if (
        proof["cold_cache_savings_proven"] is not False
        or proof["sustained_vps_coexistence_proven"] is not False
    ):
        raise CapacityProofError("UNSUPPORTED_CAPACITY_CLAIM")
    hashes = proof["evidence_sha256"]
    if (
        not isinstance(hashes, dict)
        or set(hashes) != {"before", "after"}
        or any(
            not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
            for value in hashes.values()
        )
    ):
        raise CapacityProofError("INVALID_EVIDENCE_DIGEST")
    before_peak = validate_summary(proof["before"], after=False)
    after_peak = validate_summary(proof["after"], after=True)
    with localcontext() as context:
        context.prec = 80
        saving = before_peak - after_peak
    if saving < MIN_PEAK_SAVING_MIB or decimal_value(proof["peak_saving_mib"]) != saving:
        raise CapacityProofError("PEAK_SAVING_BELOW_200_MIB")
    envelope = planning_envelope(after_peak)
    if (
        type(proof["planning_envelope_mib"]) is not int
        or proof["planning_envelope_mib"] != envelope
    ):
        raise CapacityProofError("PLANNING_FORMULA_MISMATCH")
    return envelope


def capacity_policy(proof_base64=None):
    result = {
        "mode": "default_unproven_budget",
        "planning_envelope_mib": DEFAULT_PLANNING_MIB,
        "reserve_mib": RESERVE_MIB,
        "startup_margin_mib": STARTUP_MARGIN_MIB,
        "required_memavailable_mib": DEFAULT_PLANNING_MIB + RESERVE_MIB + STARTUP_MARGIN_MIB,
        "proof_valid": None,
        "cold_cache_savings_proven": False,
        "sustained_vps_coexistence_proven": False,
    }
    if proof_base64 is None:
        return result
    try:
        if not isinstance(proof_base64, str) or len(proof_base64) > MAX_PROOF_BASE64_BYTES:
            raise CapacityProofError("EVIDENCE_TOO_LARGE")
        try:
            decoded = base64.b64decode(proof_base64, validate=True)
        except (ValueError, TypeError):
            raise CapacityProofError("INVALID_BASE64_PROOF") from None
        proof = bounded_json(decoded, MAX_PROOF_BYTES)
        envelope = validate_capacity_proof(proof)
        result.update(
            mode="measured_local_budget",
            planning_envelope_mib=envelope,
            required_memavailable_mib=envelope + RESERVE_MIB + STARTUP_MARGIN_MIB,
            proof_valid=True,
            evidence=proof,
        )
    except (CapacityProofError, TypeError, KeyError, InvalidOperation) as error:
        result.update(
            proof_valid=False,
            error_code=str(error)
            if isinstance(error, CapacityProofError)
            else "INVALID_PROOF_STRUCTURE",
        )
    return result


def call(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)


def memory():
    return {
        line.split(":")[0]: int(line.split()[1])
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines()
        if len(line.split()) > 1 and line.split()[1].isdigit()
    }


def https_status(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=15) as response:
            return response.status
    except (OSError, ValueError):
        return None


def replacement_image_transition(role, release):
    """Exact old/new owned tags for one reviewed release transition."""
    if role not in REPLACEMENT_ROLES.get(release, ()):
        raise CapacityProofError("ROLE_NOT_APPROVED_FOR_RELEASE")
    image = "quantix-frontend" if role == "frontend" else "quantix-backend"
    if release in {"stage8b1", "stage8c"}:
        return f"{image}:0.7.1-stage8a1", f"{image}:0.7.1-{release}"
    if release == "marathon":
        return f"{image}:0.7.1-stage8c", f"{image}:0.7.1-marathon"
    old_tag = "0.7.1-export2" if role in {"backend", "frontend"} else "0.7.1"
    return f"{image}:{old_tag}", f"{image}:0.7.1-stage8a1"


def replacement_images(role, release="stage8a1"):
    return set(replacement_image_transition(role, release))


def replacement_snapshot(containers, role, stopped_id, *, unhealthy_role=None):
    """Recognize exactly the owned layout; retain only non-secret identity facts."""
    owned, protected_running = {}, {}
    for container in containers:
        state = container["State"]
        labels = container["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") != "quantix":
            if state["Running"]:
                protected_running[container["Id"]] = [
                    state["StartedAt"],
                    container["RestartCount"],
                ]
            continue
        service = labels.get("com.docker.compose.service")
        if (
            service not in (*REQUIRED_ROLES, "migrate")
            or service in owned
            or container["Name"] != f"/quantix-{service}-1"
            or labels.get("com.docker.compose.container-number") != "1"
            or labels.get("com.docker.compose.oneoff", "").lower() != "false"
        ):
            raise CapacityProofError("UNEXPECTED_QUANTIX_CONTAINER_LAYOUT")
        if state["OOMKilled"] or state["Restarting"] or state["Paused"]:
            raise CapacityProofError("UNSAFE_QUANTIX_CONTAINER_STATE")
        if service in PRODUCTION_MEMORY_MIB:
            cap_bytes = PRODUCTION_MEMORY_MIB[service] * MIB
            if (
                container["HostConfig"]["Memory"] != cap_bytes
                or container["HostConfig"]["MemorySwap"] != cap_bytes
            ):
                raise CapacityProofError("QUANTIX_MEMORY_LIMIT_CHANGED")
        if service == role:
            if (
                container["Id"] != stopped_id
                or state["Status"] != "exited"
                or state["Running"]
                or state["Pid"] != 0
                or state["ExitCode"] not in (0, 143)
            ):
                raise CapacityProofError("REPLACED_CONTAINER_NOT_GRACEFULLY_STOPPED")
        elif service == "migrate":
            if state["Status"] != "exited" or state["Running"] or state["ExitCode"] != 0:
                raise CapacityProofError("MIGRATION_NOT_IDLE")
        elif (
            state["Status"] != "running"
            or not state["Running"]
            or (service != unhealthy_role and state.get("Health", {}).get("Status") != "healthy")
        ):
            raise CapacityProofError("OTHER_QUANTIX_SERVICE_NOT_HEALTHY")
        owned[service] = {
            "id": container["Id"],
            "image_id": container["Image"],
            "status": state["Status"],
            "started_at": state["StartedAt"],
            "finished_at": state["FinishedAt"],
            "restarts": container["RestartCount"],
        }
    if set(owned) != {*REQUIRED_ROLES, "migrate"}:
        raise CapacityProofError("MISSING_QUANTIX_CONTAINER")
    return {"owned": owned, "protected_running": protected_running}


def docker_data(args):
    response = call([*LOCAL_DOCKER, *args])
    if response.returncode:
        raise CapacityProofError("LOCAL_DOCKER_INSPECTION_FAILED")
    return response.stdout


def inspect_replacement(role, stopped_id, image_id, release="stage8a1"):
    ids = docker_data(["ps", "-aq", "--no-trunc"]).split()
    if not ids or len(ids) > 128:
        raise CapacityProofError("UNEXPECTED_CONTAINER_INVENTORY_SIZE")
    containers = json.loads(docker_data(["inspect", "--type", "container", *ids]))
    snapshot = replacement_snapshot(containers, role, stopped_id)
    images = json.loads(docker_data(["image", "inspect", image_id]))
    if (
        len(images) != 1
        or images[0]["Id"] != image_id
        or not replacement_images(role, release).intersection(images[0]["RepoTags"] or [])
        or snapshot["owned"][role]["image_id"] == image_id
    ):
        raise CapacityProofError("INVALID_OWNED_REPLACEMENT_IMAGE")
    return snapshot


def replacement_headroom(role, samples):
    """Use measured post-stop availability. Docker working set and cache are omitted."""
    cap = PRODUCTION_MEMORY_MIB[role]
    minimum = min(kib for _at, kib in samples)
    window = samples[-1][0] - samples[0][0]
    gaps = [right[0] - left[0] for left, right in zip(samples, samples[1:], strict=False)]
    return {
        "role_memory_cap_mib": cap,
        "reserve_mib": RESERVE_MIB,
        "startup_margin_mib": STARTUP_MARGIN_MIB,
        "active_guard_floor_mib": GUARD_SUSTAINED_FLOOR_MIB,
        "required_reserve_gate_memavailable_mib": cap + RESERVE_MIB + STARTUP_MARGIN_MIB,
        "required_guard_gate_memavailable_mib": cap
        + GUARD_SUSTAINED_FLOOR_MIB
        + STARTUP_MARGIN_MIB,
        "minimum_memavailable_mib": minimum / 1024,
        "sample_count": len(samples),
        "elapsed_seconds": round(window, 3),
        "gates": {
            "complete_post_stop_memory_window": len(samples) >= 16
            and window >= 30
            and all(0 < gap <= 5 for gap in gaps),
            "reserve_and_startup_preserved": minimum
            >= 1024 * (cap + RESERVE_MIB + STARTUP_MARGIN_MIB),
            "active_guard_headroom_preserved": minimum
            >= 1024 * (cap + GUARD_SUSTAINED_FLOOR_MIB + STARTUP_MARGIN_MIB),
        },
    }


def read_guard_identity():
    response = call(
        [
            "systemctl",
            "show",
            "--property=MainPID",
            "--property=ActiveState",
            "quantix-capacity-guard.service",
        ]
    )
    state = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
    pid = state.get("MainPID", "")
    if (
        response.returncode
        or state.get("ActiveState") != "active"
        or not pid.isdecimal()
        or int(pid) <= 0
    ):
        raise CapacityProofError("CAPACITY_GUARD_NOT_ACTIVE")
    return pid


def replacement_preflight(role, stopped_id, image_id, release="stage8a1"):
    result = {
        "kind": "READ_ONLY_QUANTIX_STOP_BEFORE_START_PREFLIGHT",
        "role": role,
        "release": release,
        "replaced_container_id": stopped_id,
        "replacement_image_id": image_id,
        "single_role_start_gate": False,
    }
    try:
        before = inspect_replacement(role, stopped_id, image_id, release)
        guard = read_guard_identity()
        nginx = call(["nginx", "-t"])
        sync = call(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        bgm = https_status("https://badguymotors.com")
        owner = https_status("https://raccncode.com")
        memory_limit, swap_limit, docker_root_name = json.loads(
            docker_data(
                [
                    "info",
                    "--format",
                    "[{{json .MemoryLimit}},{{json .SwapLimit}},{{json .DockerRootDir}}]",
                ]
            )
        )
        if memory_limit is not True or swap_limit is not True:
            raise CapacityProofError("DOCKER_MEMORY_AND_SWAP_LIMITS_NOT_ENFORCED")
        docker_root = pathlib.Path(docker_root_name)
        if not docker_root.is_absolute() or docker_root == pathlib.Path("/"):
            raise CapacityProofError("UNKNOWN_DOCKER_STORAGE")
        samples = []
        for index in range(16):
            if index:
                time.sleep(2)
            samples.append((time.monotonic(), memory()["MemAvailable"]))
        after = inspect_replacement(role, stopped_id, image_id, release)
        after_guard = read_guard_identity()
        disk_free = min(shutil.disk_usage(path).free for path in {docker_root, PRODUCTION_ROOT})
        # A final freshness check after inspection prevents a late drop from passing.
        last = (time.monotonic(), memory()["MemAvailable"])
        headroom = replacement_headroom(role, samples)
        required = headroom["required_guard_gate_memavailable_mib"]
        gates = {
            **headroom.pop("gates"),
            "current_memavailable_sufficient": last[1] >= 1024 * required,
            "inventory_stable_while_stopped": before == after,
            "capacity_guard_still_active": guard == after_guard,
            "nginx_syntax_valid": nginx.returncode == 0,
            "vps_clock_synchronized": sync.returncode == 0 and sync.stdout.strip() == "yes",
            "bgm_https_healthy": bgm == 200,
            "owner_site_https_healthy": owner == 200,
            "disk_reserve_available": disk_free >= REPLACEMENT_DISK_RESERVE_BYTES,
        }
        result.update(
            **headroom,
            gates=gates,
            single_role_start_gate=all(gates.values()),
            current_memavailable_mib=last[1] / 1024,
            disk_free_bytes=disk_free,
            bgm_status=bgm,
            owner_status=owner,
            snapshots={"before": before, "after": after},
            memory_samples_kib=[kib for _at, kib in samples],
        )
    except CapacityProofError as error:
        result["error_code"] = str(error)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        result["error_code"] = "PREFLIGHT_MEASUREMENT_FAILED"
    result["measured_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capacity-proof-b64", help="Bounded sanitized local proof; never code")
    parser.add_argument("--replacement-role", choices=REPLACEABLE_ROLES)
    parser.add_argument("--replacement-release", choices=REPLACEMENT_ROLES)
    parser.add_argument(
        "--replaced-container-id", help="Full, gracefully stopped QUANTIX container ID"
    )
    parser.add_argument("--replacement-image-id", help="Full locally loaded QUANTIX image ID")
    args = parser.parse_args()
    if any(
        (
            args.replacement_role,
            args.replaced_container_id,
            args.replacement_image_id,
            args.replacement_release,
        )
    ):
        release = args.replacement_release or "stage8a1"
        if (
            args.replacement_role not in REPLACEMENT_ROLES[release]
            or not re.fullmatch(r"[0-9a-f]{64}", args.replaced_container_id or "")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", args.replacement_image_id or "")
            or args.capacity_proof_b64 is not None
        ):
            parser.error("single-role replacement needs both full IDs and no cold-deployment proof")
        result = replacement_preflight(
            args.replacement_role, args.replaced_container_id, args.replacement_image_id, release
        )
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["single_role_start_gate"] else 1)
    policy = capacity_policy(args.capacity_proof_b64)
    test = call(["nginx", "-T"])
    files = {}
    for line in test.stdout.splitlines():
        if line.startswith("# configuration file ") and line.endswith(":"):
            path = pathlib.Path(line[len("# configuration file ") : -1])
            if path.is_file():
                files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    containers = []
    for cid in call(["docker", "ps", "-aq"]).stdout.split():
        value = json.loads(call(["docker", "inspect", cid]).stdout)[0]
        containers.append(
            {
                "id": value["Id"],
                "name": value["Name"],
                "state": value["State"]["Status"],
                "started_at": value["State"]["StartedAt"],
                "restart_count": value["RestartCount"],
                "oom": value["State"]["OOMKilled"],
            }
        )
    samples = []
    for index in range(16):
        samples.append(
            {
                "memory": memory(),
                "vmstat": pathlib.Path("/proc/vmstat").read_text(),
                "cpu": pathlib.Path("/proc/stat").read_text().splitlines()[0],
                "pressure": pathlib.Path("/proc/pressure/memory").read_text(),
                "diskstats": pathlib.Path("/proc/diskstats").read_text(),
            }
        )
        if index < 15:
            time.sleep(2)
    minimum = min(Decimal(x["memory"]["MemAvailable"]) / 1024 for x in samples)
    sync = call(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).stdout.strip()
    bgm = https_status("https://badguymotors.com")
    owner = https_status("https://raccncode.com")
    required = policy["required_memavailable_mib"]
    gates = {
        f"minimum_memavailable_{required}mib": minimum >= required,
        "vps_clock_synchronized": sync == "yes",
        "bgm_https_healthy": bgm == 200,
        "owner_site_https_healthy": owner == 200,
        "existing_nginx_syntax_valid": test.returncode == 0,
    }
    if policy["proof_valid"] is not None:
        gates["capacity_evidence_valid"] = policy["proof_valid"] is True
    print(
        json.dumps(
            {
                "kind": "READ_ONLY_PRODUCTION_PREFLIGHT",
                "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "gates": gates,
                "private_staging_gate": all(gates.values()),
                "not_public_deployment_approval": True,
                "capacity_policy": policy,
                "minimum_memavailable_mib": float(minimum),
                "containers": containers,
                "protected_config_hashes": files,
                "nginx_validation": test.stderr,
                "bgm_status": bgm,
                "owner_status": owner,
                "samples": samples,
                "disk": call(["df", "-B1", "/"]).stdout,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
