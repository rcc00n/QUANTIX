#!/usr/bin/env python3
"""Fail-safe for QUANTIX only; never start, restart or alter another workload."""

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {"db", "backend", "collector", "frontend", "notifications"}


def main():
    if ROOT != Path("/opt/quantix"):
        raise SystemExit(
            "Production guard requires the isolated /opt/quantix installation"
        )
    compose = ["docker", "compose", "-f", str(ROOT / "infra/compose.production.yaml")]
    low_samples = 0
    while True:
        memory = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        available = int(memory["MemAvailable"].split()[0]) / 1024
        disk = subprocess.run(
            ["df", "-B1", "--output=avail", str(ROOT)],
            capture_output=True,
            text=True,
            check=True,
        )
        disk_free = int(disk.stdout.splitlines()[1].strip())
        low_samples = low_samples + 1 if available < 640 else 0
        if available < 512 or low_samples >= 2 or disk_free < 5 * 1024**3:
            ids = subprocess.run(
                compose + ["ps", "-aq"], capture_output=True, text=True, check=True
            ).stdout.split()
            # Validate ownership again before any stop. Never accept a shared project.
            for cid in ids:
                item = json.loads(
                    subprocess.run(
                        ["docker", "inspect", cid],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                )[0]
                labels = item["Config"].get("Labels", {})
                if labels.get("com.docker.compose.project") != "quantix" or labels.get(
                    "com.docker.compose.service"
                ) not in SERVICES | {"migrate"}:
                    raise SystemExit("Ownership guard refused a non-QUANTIX container")
            print(
                json.dumps(
                    {
                        "event": "quantix_capacity_fail_safe",
                        "available_mib": available,
                        "disk_free_bytes": disk_free,
                        "protected_apps_untouched": True,
                    }
                ),
                flush=True,
            )
            subprocess.run(
                compose
                + [
                    "stop",
                    "-t",
                    "10",
                    "collector",
                    "notifications",
                    "backend",
                    "frontend",
                    "db",
                ],
                check=True,
            )
            # Remain stopped for explicit inspection. Never restart any workload.
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
