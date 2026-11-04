#!/usr/bin/env python3
"""Bounded genuine local UI/export workload with monotonic memory measurements."""

import argparse
import http.cookiejar
import json
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:3100"
NAMES = ["arbiq-collector-1", "arbiq-backend-1", "arbiq-db-1",
         "arbiq-frontend-1", "arbiq-notifications-1"]


def elapsed():
    return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    if not 30 <= args.seconds <= 3600:
        raise SystemExit("Measurement duration must be30..3600seconds")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        with opener.open(urllib.request.Request(BASE + "/api/" + path, data=data,
            headers={"Content-Type": "application/json", "Origin": BASE}), timeout=25) as r:
            return r.status, json.load(r)

    request("auth/login", {"username": "quantix",
                           "password": (ROOT / ".secrets/admin-password").read_text().strip()})
    samples, reads, coverage = [], [], []
    export_job = None
    start = elapsed()
    next_read = 0
    paths = ["terminal/overview", "opportunities?history=true&limit=50",
             "analytics/sessions/79bcb82e-0f13-40f2-b9f7-388f024763d8",
             "opportunities?history=true&limit=25&budget_cad=250"]
    try:
        while elapsed() - start < args.seconds:
            now = elapsed() - start
            if now >= next_read:
                path = paths[len(reads) % len(paths)]
                began = elapsed()
                try:
                    status, body = request(path)
                    if path == "terminal/overview":
                        coverage.append({k: body.get(k) for k in (
                            "as_of", "connected_exchanges", "venue_groups",
                            "discovered_markets", "monitored_markets", "compatible_intersections")})
                    del body
                except urllib.error.HTTPError as e:
                    status = e.code
                reads.append({"path": path, "status": status,
                              "latency_ms": round((elapsed()-began)*1000, 3)})
                next_read = elapsed()-start+10
            if args.export and export_job is None and now >= 30:
                output = args.output.with_name(args.output.stem + "-real-export.zip")
                export_job = subprocess.Popen([
                    "python3", str(ROOT / "scripts/export_report.py"), "--session",
                    "7214ecd9-d9af-4ad5-b467-c43ddef0aab8", "--start", "2026-11-22T05:00:00Z",
                    "--end", "2026-11-22T05:30:00Z", "--output", str(output)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            stats = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                    "{{json .}}", *NAMES], capture_output=True, text=True, check=True)
            samples.append({"elapsed_seconds": round(elapsed()-start, 3),
                            "services": [json.loads(s) for s in stats.stdout.splitlines()]})
            time.sleep(3)
        if export_job:
            export_code = export_job.wait(timeout=120)
        else:
            export_code = None
        result = {"kind": "ACTUAL_LOCAL_NORMAL_READS_AND_RETAINED_EXPORT",
                  "label": args.label, "elapsed_seconds": elapsed()-start,
                  "samples": samples, "reads": reads, "coverage": coverage,
                  "export_exit_code": export_code, "external_exchange_requests_forced": 0}
        args.output.write_text(json.dumps(result, indent=2))
        args.output.chmod(0o600)
        print(json.dumps({"label": args.label, "samples": len(samples),
                          "reads": len(reads), "status_codes": sorted({r["status"] for r in reads}),
                          "export_exit_code": export_code,
                          "median_read_ms": statistics.median(r["latency_ms"] for r in reads)}))
    finally:
        request("auth/logout", {})
        if export_job is not None and export_job.poll() is None:
            export_job.terminate()
            export_job.wait(timeout=10)


if __name__ == "__main__":
    main()
