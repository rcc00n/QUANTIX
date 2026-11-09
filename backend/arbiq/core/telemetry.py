"""Bounded process telemetry; floats are operational measurements, never money."""

import os
import resource
import time
from pathlib import Path

from arbiq.core.clock import elapsed_seconds


class ProcessTelemetry:
    def __init__(self):
        self.started = elapsed_seconds()
        self.last_elapsed = self.started
        self.last_cpu = time.process_time()
        self.cpu_pct = None

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
        }
