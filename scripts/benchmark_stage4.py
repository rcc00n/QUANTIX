#!/usr/bin/env python3
"""Controlled SYNTHETIC offline scanner/evidence capacity measurements.

No exchange connectors, credentials, remote SSH or application database writes.
Each size runs in a fresh child process so maximum RSS is comparable. Resource
samples and fixture-clock freshness are performance data, not financial outcomes.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import math
import os
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from arbiq.collector.config import CollectorConfig
from arbiq.core.clock import elapsed_seconds
from arbiq.market_data.cache import BookCache
from arbiq.market_data.discovery import DiscoveredMarket
from arbiq.market_data.identity import AssetIdentity
from arbiq.market_data.quality import safe_json, validate_book
from arbiq.opportunities.config import EngineConfig
from arbiq.opportunities.contracts import (
    Fee,
    MarketInput,
    MarketRules,
    identity_cad,
)
from arbiq.opportunities.engine import ArbitrageEngine
from arbiq.opportunities.evidence import EvidencePool, shared_json

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def rss_bytes() -> int:
    """Current Linux RSS; ru_maxrss is reported separately as process lifetime peak."""
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def synthetic_market(venue: str, index: int) -> DiscoveredMarket:
    symbol = f"SYNTH{index:02d}"
    return DiscoveredMarket(
        native_symbol=f"{symbol}/CAD",
        native_id=f"explicit-synthetic-{index}",
        base=AssetIdentity(
            f"synthetic:asset:{index}",
            symbol,
            "crypto",
            True,
            "SYNTHETIC fixture identity",
        ),
        quote=AssetIdentity(
            "fiat:CAD", "CAD", "fiat", True, "SYNTHETIC fixture CAD identity"
        ),
        active=True,
        amount_step=Decimal("0.001"),
        price_tick=Decimal("0.001"),
        min_notional=Decimal(1),
        quote_volume=Decimal(10000000),
        metadata={
            "synthetic": True,
            "active_status": "active",
            "minimum_quantity": "0.001",
        },
        id=uuid5(NAMESPACE_URL, f"quantix-explicit-synthetic/{venue}/{index}"),
    )


def synthetic_book(venue, market, now, sequence, depth=25):
    venue_index = int(venue.removeprefix("synthetic"))
    # Half the independent fixture venues have a deliberate dislocation. This
    # stresses positive research sizing rather than relying on live profits.
    mid = Decimal(100) + Decimal(market.base.symbol.removeprefix("SYNTH")) * 10
    if venue_index >= 10:
        mid *= Decimal("1.025")
    mid += Decimal(sequence % 5) * Decimal("0.001")
    raw = {
        "symbol": market.native_symbol,
        "timestamp": int(now.timestamp() * 1000),
        "bids": [
            [str(mid - Decimal("0.01") - Decimal(i) * Decimal("0.001")), "10"]
            for i in range(depth)
        ],
        "asks": [
            [str(mid + Decimal("0.01") + Decimal(i) * Decimal("0.001")), "10"]
            for i in range(depth)
        ],
    }
    book = validate_book(
        raw,
        market_id=market.id,
        exchange=venue,
        symbol=market.native_symbol,
        base_key=market.base.key,
        quote_key=market.quote.key,
        received_at=now,
        request_started_at=now - timedelta(milliseconds=20),
        latency_ms=20,
        config=CollectorConfig(depth=depth),
        source="SYNTHETIC_OFFLINE_REPLAY",
    )
    return book.model_copy(update={"ingestion_index": sequence})


