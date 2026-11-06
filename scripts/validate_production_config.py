#!/usr/bin/env python3
"""Validate normalized Compose mount/isolation contracts without printing env/secrets."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    raw = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "infra/compose.production.yaml"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(raw.stdout)
    services = config["services"]
    assert config["name"] == "quantix"
    assert set(services) == {
        "db",
        "migrate",
        "backend",
        "collector",
        "frontend",
        "notifications",
    }
    assert config["networks"]["data"]["internal"] is True
    for name, service in services.items():
        assert service["image"].startswith("quantix-"), "Refuse shared image tags"
        assert int(service["mem_limit"]) == int(service["memswap_limit"]), (
            "No QUANTIX swap allowed"
        )
        assert not service.get("privileged", False)
        assert all(
            isinstance(mount, str) and mount.startswith("/")
            for mount in service.get("tmpfs", [])
        ), "Invalid tmpfs path; quote comma-containing YAML entries"
        assert all(
            "docker.sock" not in str(mount) for mount in service.get("volumes", [])
        )
        if name != "db":
            assert service["read_only"] is True
            assert "ALL" in service["cap_drop"]
        if name == "frontend":
            ports = service["ports"]
            assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1"
            assert str(ports[0]["published"]) == "3310"
        else:
            assert not service.get("ports")
        if name not in {"db", "frontend"}:
            assert service["environment"]["TRADING_MODE"] == "paper"
            assert service["environment"]["AI_ENABLED"] == "false"
        if name != "notifications":
            assert all(
                s["source"] != "telegram_bot_token" for s in service.get("secrets", [])
            )
    print(
        json.dumps(
            {
                "production_config": "PASS",
                "services": len(services),
                "public_internal_ports": 0,
                "swap_allowed": False,
                "environment_values_printed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
