#!/usr/bin/env python3
"""One real public discovery pass; lossless-cache before/after, never trade traffic."""

import argparse
import asyncio
import ctypes
import gc
import hashlib
import json
import pickle
from pathlib import Path

from arbiq.collector.config import DEFAULT_EXCHANGES, CollectorConfig
from arbiq.connectors.ccxt_public import PublicConnector, classify_error
from arbiq.connectors.metadata import FIELDS
from arbiq.connectors.registry import capabilities
from arbiq.market_data.discovery import normalize_market, selected_markets


def memory():
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        key, *rest = line.split()
        if rest and rest[0].isdigit():
            values[key.rstrip(":")] = int(rest[0]) * 1024
    return values


