#!/usr/bin/env python3
"""Check QUANTIX's additive NGINX host; only --apply may send one graceful HUP.

The root-owned baseline pins the existing loaded file order, SHA-256 values and
warning multiset. A later unrelated change fails closed. Rebaselining requires
independent inspection; this helper never rewrites a baseline or restarts NGINX.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

BASELINE = Path("/opt/quantix/safety/nginx-baseline.json")
VHOST = "/etc/nginx/conf.d/zz-quantix.conf"
MAX_OUTPUT = 4 * 1024 * 1024
PROTECTED_HTTPS = ("https://badguymotors.com", "https://raccncode.com")
QUANTIX_CERTBOT = Path("/opt/quantix/certbot")


class GuardError(Exception):
    def __init__(self, code, *, reload_applied=False):
        self.code, self.reload_applied = code, reload_applied
        super().__init__(code)


def run(args, *, timeout=20):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GuardError("COMMAND_FAILED") from None
    if len(result.stdout) + len(result.stderr) > MAX_OUTPUT:
        raise GuardError("COMMAND_OUTPUT_LIMIT")
    return result


