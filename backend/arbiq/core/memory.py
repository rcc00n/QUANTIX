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


