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










