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










