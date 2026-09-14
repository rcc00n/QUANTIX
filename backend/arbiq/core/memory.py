"""Release only unreachable objects and unused libc pages in this process.

No market cache, financial evidence, kernel cache or other process is cleared.
The optional glibc operation is a no-op on runtimes that do not provide it.
"""

import asyncio
import ctypes
import gc
import os
import time
from pathlib import Path


def rss_bytes():
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def reclaim_unused_pages():
    before = rss_bytes()
    started = time.monotonic()
    collected = gc.collect()
    supported = False
    try:
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)
        supported = True
    except (AttributeError, OSError):
        pass
    after = rss_bytes()
    return {
        "before_rss_bytes": before,
        "after_rss_bytes": after,
        "unused_pages_released_bytes": max(0, before - after)
        if before is not None and after is not None
        else None,
        "gc_unreachable_collected": collected,
        "libc_trim_supported": supported,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "scope": "unreachable objects and unused allocator pages only",
    }


