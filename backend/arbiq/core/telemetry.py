"""Bounded process telemetry; floats are operational measurements, never money."""

import os
import resource
import time
from pathlib import Path

from arbiq.core.clock import elapsed_seconds


def _cgroup_path():
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::/"):
                return Path("/sys/fs/cgroup") / line.removeprefix("0::/")
    except OSError:
        pass
    return None


class CgroupTelemetry:
    def __init__(self, root: Path | None):
        self.root = root
        self.previous = None
        self.snapshot = None

    def _number(self, name: str):
        try:
            value = int((self.root / name).read_text())
            return value if value >= 0 else None
        except (OSError, ValueError):
            return None  # Missing controller, or "max" for an unlimited cgroup.

    def _counters(self, name: str):
        try:
            return {
                key: int(value)
                for key, value in (
                    line.split() for line in (self.root / name).read_text().splitlines()
                )
            }
        except (OSError, ValueError):
            return {}

    def _cpu_limit(self):
        try:
            quota, period = (self.root / "cpu.max").read_text().split()
            quota, period = int(quota), int(period)
            if quota > 0 and period > 0:
                return quota / period
        except (OSError, ValueError):
            pass
        return None

    def sample(self, now: float):
        if self.root is None:
            return None
        if self.previous and now - self.previous[0] < 1:
            return self.snapshot
        charged = self._number("memory.current")
        memory_limit = self._number("memory.max")
        cpu_limit = self._cpu_limit()
        events = self._counters("memory.events")
        cpu = self._counters("cpu.stat")
        if charged is None and not cpu:
            return None

        throttle_pct = cpu_share_pct = None
        if self.previous:
            stamp, previous = self.previous
            interval = now - stamp
            if all("usage_usec" in value for value in (cpu, previous)) and cpu_limit:
                usage = cpu["usage_usec"] - previous["usage_usec"]
                if usage >= 0:
                    cpu_share_pct = round(100 * usage / (interval * 1_000_000 * cpu_limit), 3)
            keys = ("nr_periods", "nr_throttled")
            if all(key in value for value in (cpu, previous) for key in keys):
                periods, throttles = (cpu[key] - previous[key] for key in keys)
                if periods > 0 and 0 <= throttles <= periods:
                    throttle_pct = round(100 * throttles / periods, 3)
        self.previous = now, cpu
        self.snapshot = {
            "scope": "current_process_cgroup_v2",
            "cpu_limit_cores": cpu_limit,
            "cpu_pct_of_limit": cpu_share_pct,
            "cpu_throttled_period_pct": throttle_pct,
            "memory_charged_bytes": charged,
            "memory_limit_bytes": memory_limit,
            "memory_pct_of_limit": round(100 * charged / memory_limit, 3)
            if charged is not None and memory_limit
            else None,
            "memory_limit_events_total": events.get("max"),
            "oom_kills_total": events.get("oom_kill"),
        }
        return self.snapshot


class ProcessTelemetry:
    def __init__(self):
        self.started = elapsed_seconds()
        self.last_elapsed = self.started
        self.last_cpu = time.process_time()
        self.cpu_pct = None
        self.cgroup = CgroupTelemetry(_cgroup_path())

    def sample(self):
        now, cpu = elapsed_seconds(), time.process_time()
        if now - self.last_elapsed >= 1:
            self.cpu_pct = round(100 * (cpu - self.last_cpu) / (now - self.last_elapsed), 3)
            self.last_elapsed, self.last_cpu = now, cpu
        try:
            rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            rss = None
        return {
            "current_rss_bytes": rss,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "cpu_seconds": round(cpu, 4),
            "cpu_pct": self.cpu_pct,
            "uptime_seconds": round(now - self.started, 3),
            "source": "local_process_procfs_and_cpu_clock",
            "cpu_convention": "percent of one CPU over interval between samples; not host load",
            "cgroup": self.cgroup.sample(now),
        }
