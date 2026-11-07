"""Pure SYNTHETIC resource guard fixtures; no Docker, SSH, network or financial DB."""

import ast
import base64
import copy
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if not SCRIPTS.is_dir():
    SCRIPTS = Path("/app/scripts")


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = load_script("production_preflight")
policy = load_script("capacity_policy")


def synthetic_profile(*, after):
    duration = 901 if after else 181
    allocations = [400, 150, 300, 100, 50] if after else [550, 300, 400, 100, 50]
    roles = ["collector", "backend", "db", "frontend", "notifications"]
    services = [
        {
            "Name": f"arbiq-{role}-1",
            "ID": f"{i + 1:012x}",
            "MemUsage": f"{amount}MiB / 8GiB",
        }
        for i, (role, amount) in enumerate(zip(roles, allocations, strict=True))
    ]
    return {
        "kind": policy.PROFILE_KIND,
        "label": "EXPLICITLY_SYNTHETIC_RESOURCE_FIXTURE",
        "elapsed_seconds": duration,
        "samples": [
            {"elapsed_seconds": second, "services": copy.deepcopy(services)}
            for second in range(0, duration, 30)
        ],
        "reads": [
            {"path": path, "status": 200}
            for path in (
                "terminal/overview",
                "opportunities?history=true&limit=50",
                "opportunities?history=true&limit=25&budget_cad=250",
                "analytics/sessions/00000000-0000-0000-0000-000000000000",
            )
        ],
        "coverage": [{"connected_exchanges": 20, "venue_groups": 19, "monitored_markets": 200}],
        "export_exit_code": 0,
        "external_exchange_requests_forced": 0,
    }


def make_proof(before=None, after=None):
    return policy.build_capacity_proof(
        json.dumps(before or synthetic_profile(after=False)).encode(),
        json.dumps(after or synthetic_profile(after=True)).encode(),
    )


def encoded(proof):
    return base64.b64encode(preflight.canonical(proof)).decode()


def test_no_proof_preserves_original_1536_plus_512_plus_64_gate():
    value = preflight.capacity_policy()
    assert value["planning_envelope_mib"] == 1536
    assert value["reserve_mib"] == 512 and value["startup_margin_mib"] == 64
    assert value["required_memavailable_mib"] == 2112
    assert value["proof_valid"] is None


def test_valid_proof_recomputes_budget_and_preserves_safety_and_hashes():
    proof = make_proof()
    value = preflight.capacity_policy(encoded(proof))
    assert value["proof_valid"] is True
    assert proof["after"]["stack_peak_mib"] == "1000"
    assert proof["peak_saving_mib"] == "400"
    assert value["planning_envelope_mib"] == 1152
    assert value["required_memavailable_mib"] == 1728
    assert len(proof["evidence_sha256"]["before"]) == 64
    assert proof["cold_cache_savings_proven"] is False
    assert proof["sustained_vps_coexistence_proven"] is False
    assert "00000000-0000" not in json.dumps(proof)
    assert "SYNTHETIC_RESOURCE_FIXTURE" not in json.dumps(proof)


@pytest.mark.parametrize(
    "peak,expected",
    [
        ("1000", 1152),
        ("1280", 1408),
        ("1280.000000000000000001", 1472),
        ("1600", 1792),
    ],
)
def test_rounding_is_upward_to_64_and_uses_larger_margin(peak, expected):
    assert preflight.planning_envelope(peak) == expected


@pytest.mark.parametrize(
    "fault",
    [
        "short",
        "read_failed",
        "export_failed",
        "missing_export",
        "coverage_adapter",
        "coverage_group",
        "coverage_book",
        "restart",
        "missing_service",
        "duplicate_service",
        "out_of_order",
        "unobserved_tail",
        "missing_read_category",
        "forced_external",
        "weak_saving",
    ],
)
def test_incomplete_or_degraded_evidence_cannot_lower_budget(fault):
    after = synthetic_profile(after=True)
    if fault == "short":
        after["elapsed_seconds"] = 899
        after["samples"].pop()
    elif fault == "read_failed":
        after["reads"][0]["status"] = 429
    elif fault == "export_failed":
        after["export_exit_code"] = 1
    elif fault == "missing_export":
        after["export_exit_code"] = None
    elif fault.startswith("coverage_"):
        field = {
            "coverage_adapter": "connected_exchanges",
            "coverage_group": "venue_groups",
            "coverage_book": "monitored_markets",
        }[fault]
        after["coverage"][0][field] -= 1
    elif fault == "restart":
        after["samples"][-1]["services"][0]["ID"] = "ffffffffffff"
    elif fault == "missing_service":
        after["samples"][-1]["services"].pop()
    elif fault == "duplicate_service":
        after["samples"][-1]["services"][-1] = after["samples"][-1]["services"][0]
    elif fault == "out_of_order":
        after["samples"][-1]["elapsed_seconds"] = 0
    elif fault == "unobserved_tail":
        after["elapsed_seconds"] = 2000
    elif fault == "missing_read_category":
        after["reads"][-1]["path"] = "terminal/overview"
    elif fault == "forced_external":
        after["external_exchange_requests_forced"] = 1
    else:
        for sample in after["samples"]:
            sample["services"][0]["MemUsage"] = "601MiB / 8GiB"
    with pytest.raises(preflight.CapacityProofError):
        make_proof(after=after)


@pytest.mark.parametrize(
    "fault", ["reserve", "startup", "budget", "duration", "saving", "claim", "unknown"]
)
def test_resealed_tampering_still_fails_formula_and_policy_guards(fault):
    proof = make_proof()
    key, value = {
        "reserve": ("reserve_mib", 0),
        "startup": ("startup_margin_mib", 0),
        "budget": ("planning_envelope_mib", 64),
        "duration": ("minimum_after_seconds", 100),
        "saving": ("minimum_peak_saving_mib", 1),
        "claim": ("cold_cache_savings_proven", True),
        "unknown": ("untrusted_text", "SYNTHETIC-secret"),
    }[fault]
    proof[key] = value
    value = preflight.capacity_policy(encoded(preflight.seal_proof(proof)))
    assert value["proof_valid"] is False
    assert value["required_memavailable_mib"] == 2112
    assert "SYNTHETIC-secret" not in json.dumps(value)


@pytest.mark.parametrize(
    "value", ["!not-base64!", "e30=", "x" * (preflight.MAX_PROOF_BASE64_BYTES + 1)]
)
def test_invalid_or_oversized_proof_blocks_and_keeps_default(value):
    result = preflight.capacity_policy(value)
    assert result["proof_valid"] is False
    assert result["planning_envelope_mib"] == 1536


def test_numeric_units_are_converted_without_float_or_decimal_parity_assumptions():
    assert policy.docker_memory_mib("1GiB / 8GiB") == 1024
    assert policy.docker_memory_mib("1048576B / 8GiB") == 1
    assert policy.docker_memory_mib("1MB / 8GiB") == Decimal("0.95367431640625")
    with pytest.raises(preflight.CapacityProofError):
        policy.docker_memory_mib("NaNMiB / 8GiB")


def test_remote_script_remains_standalone_python310_and_never_executes_proof_code():
    for name in ("capacity_policy", "production_preflight"):
        ast.parse((SCRIPTS / (name + ".py")).read_text(), feature_version=(3, 10))
    source = (SCRIPTS / "production_preflight.py").read_text()
    assert "import arbiq" not in source
    assert "import capacity_policy" not in source
    assert "exec(" not in source and "eval(" not in source


def replacement_inventory(role="collector"):
    """Synthetic containers matching the owned Compose labels and bounded limits."""
    containers = []
    for index, service in enumerate((*preflight.REQUIRED_ROLES, "migrate"), 1):
        idle = service in {role, "migrate"}
        cap = preflight.PRODUCTION_MEMORY_MIB.get(service, 192) * preflight.MIB
        containers.append(
            {
                "Id": f"{index:064x}",
                "Name": f"/quantix-{service}-1",
                "Image": "sha256:" + "a" * 64,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "quantix",
                        "com.docker.compose.service": service,
                        "com.docker.compose.container-number": "1",
                        "com.docker.compose.oneoff": "False",
                    }
                },
                "State": {
                    "Status": "exited" if idle else "running",
                    "Running": not idle,
                    "Restarting": False,
                    "Paused": False,
                    "OOMKilled": False,
                    "ExitCode": 0,
                    "Pid": 0 if idle else index,
                    "StartedAt": "SYNTHETIC_START",
                    "FinishedAt": "SYNTHETIC_END" if idle else "",
                    "Health": {"Status": "healthy"},
                },
                "HostConfig": {"Memory": cap, "MemorySwap": cap},
                "RestartCount": 0,
            }
        )
    return containers


@pytest.mark.parametrize(
    "role,reserve_required,guard_required",
    [
        ("collector", 1344, 1472),
        ("backend", 960, 1088),
        ("frontend", 768, 896),
        ("notifications", 736, 864),
    ],
)
def test_replacement_requires_one_full_service_cap_above_real_post_stop_memory_floor(
    role, reserve_required, guard_required
):
    result = preflight.replacement_headroom(
        role, [(i * 2, 1024 * guard_required) for i in range(16)]
    )
    assert result["required_reserve_gate_memavailable_mib"] == reserve_required
    assert result["required_guard_gate_memavailable_mib"] == guard_required
    assert all(result["gates"].values())


def test_one_low_post_stop_sample_fails_even_if_instant_reserve_gate_passes():
    samples = [(i * 2, 1472 * 1024) for i in range(16)]
    samples[6] = (12, 1472 * 1024 - 1)
    result = preflight.replacement_headroom("collector", samples)
    assert result["gates"]["reserve_and_startup_preserved"]
    assert not result["gates"]["active_guard_headroom_preserved"]


@pytest.mark.parametrize("times", [range(0, 16, 1), range(0, 32, 3), range(0, 128, 8)])
def test_high_memory_in_incomplete_window_cannot_pass_replacement(times):
    result = preflight.replacement_headroom("collector", [(t, 9999 * 1024) for t in times])
    assert not result["gates"]["complete_post_stop_memory_window"]


def test_snapshot_pins_old_container_and_leaves_protected_apps_read_only():
    inventory = replacement_inventory()
    protected = copy.deepcopy(inventory[0])
    protected["Id"] = "e" * 64
    protected["Config"]["Labels"] = None
    protected["Config"]["Env"] = ["SYNTHETIC_PRIVATE_SETTING=fixture"]
    inventory.append(protected)
    snapshot = preflight.replacement_snapshot(inventory, "collector", inventory[1]["Id"])
    assert snapshot["owned"]["collector"]["status"] == "exited"
    assert snapshot["protected_running"] == {protected["Id"]: ["SYNTHETIC_START", 0]}
    assert "SYNTHETIC_PRIVATE_SETTING" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_id",
        "target_running",
        "duplicate",
        "other_service_down",
        "unexpected_job",
        "target_killed",
        "weakened_cap",
        "swap_allowed",
        "migration_started",
    ],
)
def test_invalid_runtime_shape_cannot_pass_replacement(fault):
    inventory = replacement_inventory()
    old = inventory[1]
    identifier = old["Id"]
    if fault == "wrong_id":
        identifier = "b" * 64
    elif fault == "target_running":
        old["State"].update(Status="running", Running=True, Pid=123)
    elif fault == "duplicate":
        inventory.append(copy.deepcopy(old))
    elif fault == "other_service_down":
        inventory[0]["State"]["Health"]["Status"] = "unhealthy"
    elif fault == "unexpected_job":
        job = copy.deepcopy(old)
        job["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
        inventory.append(job)
    elif fault == "target_killed":
        old["State"]["ExitCode"] = 137
    elif fault == "weakened_cap":
        old["HostConfig"]["Memory"] += 64 * preflight.MIB
    elif fault == "swap_allowed":
        old["HostConfig"]["MemorySwap"] = -1
    else:
        inventory[-1]["State"].update(Status="running", Running=True, Pid=123)
    with pytest.raises(preflight.CapacityProofError):
        preflight.replacement_snapshot(inventory, "collector", identifier)
