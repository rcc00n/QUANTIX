"""Native Kraken v2 L2 snapshots/deltas with exact-decimal CRC32 validation."""

import asyncio
import json
import zlib
from decimal import Decimal

from websockets.asyncio.client import connect

from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import retry_delay
from arbiq.core.clock import elapsed_seconds
from arbiq.finance.contracts import exact_decimal


class ResynchronizationRequired(ValueError):
    pass


class SequenceGuard:
    """For feeds that advertise contiguous sequence numbers; never apply to REST nonces."""

    def __init__(self):
        self.last: int | None = None

    def accept(self, sequence: int, snapshot: bool = False):
        if not snapshot and (self.last is None or sequence != self.last + 1):
            raise ResynchronizationRequired("SEQUENCE_GAP")
        self.last = sequence


