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


class KrakenBook:
    def __init__(self, depth: int):
        self.depth = depth
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.initialized = False

    @staticmethod
    def _digits(value: Decimal) -> str:
        return format(value, "f").replace(".", "").lstrip("0")

    def checksum(self) -> int:
        content = ""
        for side, reverse in ((self.asks, False), (self.bids, True)):
            for price, quantity in sorted(side.items(), reverse=reverse)[:10]:
                content += self._digits(price) + self._digits(quantity)
        return zlib.crc32(content.encode()) & 0xFFFFFFFF

    def apply(self, data: dict, snapshot: bool) -> dict:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
            self.initialized = True
        elif not self.initialized:
            raise ResynchronizationRequired("DELTA_WITHOUT_SNAPSHOT")
        try:
            for key in ("bids", "asks"):
                side = getattr(self, key)
                for level in data.get(key, []):
                    price, quantity = exact_decimal(level["price"]), exact_decimal(level["qty"])
                    if price <= 0 or quantity < 0:
                        raise ValueError("Invalid WS level")
                    if quantity == 0:
                        side.pop(price, None)
                    else:
                        side[price] = quantity
                limited = dict(sorted(side.items(), reverse=key == "bids")[: self.depth])
                setattr(self, key, limited)
            expected = data.get("checksum")
            if expected is None or self.checksum() != int(expected):
                raise ResynchronizationRequired("CHECKSUM_MISMATCH")
        except (ValueError, KeyError, TypeError):
            self.initialized = False
            raise
        return {
            "symbol": data["symbol"],
            "timestamp": data.get("timestamp"),
            "bids": [[p, q] for p, q in sorted(self.bids.items(), reverse=True)],
            "asks": [[p, q] for p, q in sorted(self.asks.items())],
            "nonce": None,
        }


