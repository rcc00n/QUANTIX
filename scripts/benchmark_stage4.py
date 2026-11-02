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


class SyntheticStore:
    def __init__(self):
        self.saved = 0
        self.evidence_bytes = 0
        self.statuses = Counter()
        self.route_ids = {}

    async def save(self, key, payload, buy, sell, *args):
        now = payload["evidence"]["calculation_inputs"]["evaluated_at"]
        body = safe_json(
            {
                **payload,
                "id": self.route_ids.setdefault(key, str(uuid4())),
                "detected_at": now,
                "updated_at": now,
            }
        )
        serialized = json.dumps(body, separators=(",", ":"))
        self.evidence_bytes += len(serialized.encode())
        self.saved += 1
        self.statuses[body["status"]] += 1
        return body

    async def transition(self, *args):
        return None

    async def expire_previous_runs(self, *args):
        return None


async def scanner_measurement(count: int, seconds: float) -> dict:
    if count not in (120, 200, 300, 500):
        raise ValueError("Supported controlled book counts:120/200/300/500")
    slugs = [f"synthetic{i:02d}" for i in range(20)]
    config = CollectorConfig(
        exchange_ids=slugs, max_markets_per_exchange=math.ceil(count / 20)
    )
    cache = BookCache(config)
    runtimes = {
        slug: SimpleNamespace(
            slug=slug,
            exchange_id=uuid4(),
            markets=[],
            catalog=[],
            health={"error_code": None},
        )
        for slug in slugs
    }
    flat = []
    for i in range(count):
        slug = slugs[i % 20]
        market = synthetic_market(slug, i // 20)
        runtimes[slug].markets.append(market)
        runtimes[slug].catalog.append(market)
        flat.append((slug, market))
    started = elapsed_seconds()
    now = lambda: FIXTURE_TIME + timedelta(seconds=elapsed_seconds() - started)
    cache.now = now
    for index, (slug, market) in enumerate(flat):
        cache.put(synthetic_book(slug, market, now(), index + 1))
    requested = set()

    def request(mid):
        if mid in requested:
            return "COOLDOWN"
        requested.add(mid)
        return "QUEUED"

    collector = SimpleNamespace(
        settings=SimpleNamespace(arbitrage=EngineConfig()),
        store=SimpleNamespace(sessions=None, run_id=uuid4()),
        cache=cache,
        runtimes=runtimes,
        utcnow=now,
        utc_clock=SimpleNamespace(
            snapshot=lambda: {"trusted": True, "host_healthy": True}
        ),
        request_confirmation=request,
        stop=asyncio.Event(),
    )
    engine = ArbitrageEngine(collector)
    store = SyntheticStore()
    engine.store = store
    for book in cache.books.values():
        engine.changed(book)
    task = asyncio.create_task(engine.run())
    samples = []
    observations = count
    update_index = 0
    next_update = elapsed_seconds()
    next_sample = elapsed_seconds()
    measured_start = elapsed_seconds()
    cpu_start = time.process_time()
    while elapsed_seconds() - measured_start < seconds:
        wall = elapsed_seconds()
        # Replay a normal25-second per-book cadence; never accelerate exchange
        # HTTP/subscriptions to manufacture load. All observations are local.
        while wall >= next_update:
            slug, market = flat[update_index % count]
            observations += 1
            update_index += 1
            book = cache.put(synthetic_book(slug, market, now(), observations))
            engine.changed(book)
            next_update += 25 / count
        if wall >= next_sample:
            samples.append(
                {
                    "elapsed_seconds": wall - measured_start,
                    "rss_bytes": rss_bytes(),
                    "backlog": len(engine.jobs),
                    "financial_evaluations": engine.metrics["evaluations"],
                }
            )
            next_sample += 1
        await asyncio.sleep(0.02)
    collector.stop.set()
    await asyncio.wait_for(task, 15)
    elapsed = elapsed_seconds() - measured_start
    cpu = time.process_time() - cpu_start
    ages = [(now() - book.received_at).total_seconds() for book in cache.books.values()]
    output = {
        "kind": "EXPLICITLY_SYNTHETIC_OFFLINE_CAPACITY",
        "external_requests": 0,
        "book_count": count,
        "depth": 25,
        "duration_seconds": elapsed,
        "process_cpu_seconds": cpu,
        "one_cpu_utilization_pct": cpu / elapsed * 100,
        "process_rss_bytes": rss_bytes(),
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * 1024,
        "rss_samples": samples,
        "rss_change_first_last_bytes": samples[-1]["rss_bytes"]
        - samples[0]["rss_bytes"]
        if samples
        else None,
        "observations_ingested": observations,
        "cache_books": len(cache.books),
        "cache_capacity": cache.limit,
        "source_quality_counts": dict(
            Counter(book.state.value for book in cache.books.values())
        ),
        "fixture_clock_ingestion_age_seconds": {
            "median": statistics.median(ages),
            "max": max(ages),
        },
        "engine": engine.diagnostics(),
        "synthetic_evidence_writes": store.saved,
        "serialized_evidence_bytes": store.evidence_bytes,
        "postgres_growth": "not measured by this memory-only scanner replay",
        "rate_limit_errors": "not applicable: no exchange API traffic",
        "interpretation": "CPU/RSS of scanner/cache/math process only; add measured live adapter/API/PostgreSQL/frontend footprint separately",
    }
    return output


