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


def evidence_measurement(count: int, shared: bool) -> dict:
    """Same exact route payloads before/after sharing, in separate processes."""
    gc.collect()
    tracemalloc.start()
    start = elapsed_seconds()
    pool = EvidencePool(20)
    markets = [synthetic_market(f"synthetic{i:02d}", 0) for i in range(20)]
    books = [
        synthetic_book(f"synthetic{i:02d}", market, FIXTURE_TIME, 1)
        for i, market in enumerate(markets)
    ]
    inputs = [
        MarketInput(
            str(m.id),
            f"synthetic{i:02d}",
            m.base.key,
            "CAD",
            b.normalized.asks,
            b.normalized.bids,
            Fee(Decimal("0.006")),
            MarketRules(amount_step=Decimal("0.001")),
            identity_cad(),
        )
        for i, (m, b) in enumerate(zip(markets, books, strict=True))
    ]
    fx = {str(book.market_id): pool.book(book) for book in books[:5]}
    retained = []
    payload = None
    for i in range(count):
        payload = {
            "quantity": Decimal("0.01"),
            "status": "RESEARCH_ONLY",
            "synthetic": True,
            "evidence": {
                "fx_books": fx,
                "calculation_inputs": {
                    "buy": pool.market_input(inputs[i % 20]),
                    "sell": pool.market_input(inputs[(i + 1) % 20]),
                    "evaluated_at": FIXTURE_TIME.isoformat(),
                },
            },
        }
        retained.append(
            pool.current(shared_json(payload), payload)
            if shared
            else safe_json(payload)
        )
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # Canonical payload comparison avoids comparing Python tuple/list ownership.
    expected = json.dumps(safe_json(payload), sort_keys=True, separators=(",", ":"))
    actual = json.dumps(retained[-1], sort_keys=True, separators=(",", ":"))
    return {
        "kind": "EXPLICITLY_SYNTHETIC_IDENTICAL_EVIDENCE_MEMORY",
        "variant": "shared_immutable" if shared else "independent_deep_copies",
        "routes": count,
        "traced_current_bytes": current,
        "traced_peak_bytes": peak,
        "duration_seconds": elapsed_seconds() - start,
        "canonical_payload_equal": actual == expected,
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * 1024,
        "pool": pool.metrics(),
    }


async def paper_ledger_measurement(rounds: int) -> dict:
    """Exercise real balanced SQL journals only in an explicitly isolated _test DB."""
    from arbiq.db.models import (
        Asset,
        Exchange,
        Journal,
        LedgerEntry,
        PaperSession,
        StrategyVersion,
        User,
    )
    from arbiq.portfolio.accounting import (
        execute_leg,
        fund,
        reconcile,
        release_remaining,
        reserve,
    )
    from arbiq.portfolio.contracts import PriceEvidence, Reservation
    from arbiq.portfolio.valuation import snapshot
    from sqlalchemy import func, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url or not (make_url(url).database or "").endswith("_test"):
        raise ValueError(
            "TEST_DATABASE_URL must point to an isolated database ending _test"
        )
    if not 1 <= rounds <= 1000:
        raise ValueError("Paper benchmark rounds must be1–1000")
    engine = create_async_engine(url, pool_size=2, max_overflow=0, hide_parameters=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid4().hex
    evidence = {
        "synthetic": True,
        "kind": "SYNTHETIC_ACCOUNTING_CAPACITY",
        "no_exchange_requests": True,
    }
    size_sql = text(
        "SELECT sum(pg_total_relation_size(quote_ident(schemaname)||'.'||quote_ident(tablename))) FROM pg_tables WHERE schemaname='public'"
    )
    try:
        async with maker() as db:
            before = await db.scalar(size_sql)
            user = User(
                username="synthetic-capacity-" + nonce,
                password_hash="SYNTHETIC_NOT_A_LOGIN",
            )
            venues = [
                Exchange(
                    slug=f"synthetic-capacity-{i}-{nonce}",
                    name="SYNTHETIC capacity venue",
                )
                for i in range(2)
            ]
            cad = await db.scalar(
                select(Asset).where(Asset.canonical_key == "fiat:CAD")
            )
            if cad is None:
                cad = Asset(
                    canonical_key="fiat:CAD",
                    symbol="CAD",
                    kind="fiat",
                    identifiers={"synthetic": True},
                )
                db.add(cad)
            base = Asset(
                canonical_key="synthetic:capacity:" + nonce,
                symbol="SYNTH_BTC",
                kind="crypto",
                identifiers=evidence,
            )
            db.add_all([user, *venues, base])
            await db.flush()
            strategy = StrategyVersion(
                user_id=user.id,
                version=1,
                settings=evidence,
                reason="SYNTHETIC benchmark",
            )
            db.add(strategy)
            await db.flush()
            session = PaperSession(
                user_id=user.id,
                strategy_version_id=strategy.id,
                name="SYNTHETIC capacity research",
                started_at=FIXTURE_TIME,
                initial_equity_cad=Decimal(10000),
                benchmark_snapshot={"synthetic": True, "research_only": True},
            )
            db.add(session)
            await db.flush()
            await fund(
                db,
                session.id,
                venues[0].id,
                cad.id,
                Decimal(5000),
                Decimal(5000),
                FIXTURE_TIME,
                "capacity:cash:" + nonce,
                evidence,
            )
            await fund(
                db,
                session.id,
                venues[1].id,
                base.id,
                Decimal(50),
                Decimal(5000),
                FIXTURE_TIME,
                "capacity:base:" + nonce,
                evidence,
            )
            await db.commit()
            started = elapsed_seconds()
            cpu_started = time.process_time()
            latencies = []
            initial_rss = rss_bytes()
            for i in range(rounds):
                tick = elapsed_seconds()
                now = FIXTURE_TIME + timedelta(seconds=i + 1)
                key = f"capacity:{nonce}:{i}"
                await reserve(
                    db,
                    session.id,
                    [
                        Reservation(venues[0].id, cad.id, Decimal(2)),
                        Reservation(venues[1].id, base.id, Decimal("0.01")),
                    ],
                    now,
                    key,
                    evidence,
                )
                await db.commit()
                await execute_leg(
                    db,
                    session.id,
                    venues[0].id,
                    base.id,
                    cad.id,
                    "buy",
                    Decimal("0.01"),
                    Decimal("1.01"),
                    Decimal("0.00606"),
                    "quote",
                    Decimal(100),
                    Decimal(1),
                    now,
                    key + ":buy",
                    None,
                    evidence,
                    reservation_key=key,
                )
                await db.commit()
                await execute_leg(
                    db,
                    session.id,
                    venues[1].id,
                    base.id,
                    cad.id,
                    "sell",
                    Decimal("0.01"),
                    Decimal("1.03"),
                    Decimal("0.00618"),
                    "quote",
                    Decimal(100),
                    Decimal(1),
                    now,
                    key + ":sell",
                    None,
                    evidence,
                    reservation_key=key,
                )
                await db.commit()
                await release_remaining(
                    db, session.id, key, now, key + ":release", evidence
                )
                await db.commit()
                latencies.append((elapsed_seconds() - tick) * 1000)
            now = FIXTURE_TIME + timedelta(seconds=rounds + 1)
            mark = PriceEvidence(
                Decimal(100), now, now, False, "SYNTHETIC capacity mark", evidence
            )
            valuation = await snapshot(
                db,
                session.id,
                {(venue.id, base.id): mark for venue in venues},
                now,
                research_only=True,
            )
            await db.commit()
            reconciliation = await reconcile(db, session.id)
            after = await db.scalar(size_sql)
            journal_count = await db.scalar(
                select(func.count())
                .select_from(Journal)
                .where(Journal.session_id == session.id)
            )
            ledger_count = await db.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .join(Journal)
                .where(Journal.session_id == session.id)
            )
            elapsed = elapsed_seconds() - started
            return {
                "kind": "EXPLICITLY_SYNTHETIC_POSTGRES_PAPER_ACCOUNTING",
                "rounds": rounds,
                "session_id": str(session.id),
                "external_requests": 0,
                "actual_exchange_orders": 0,
                "duration_seconds": elapsed,
                "process_cpu_seconds": time.process_time() - cpu_started,
                "roundtrip_latency_ms": {
                    "mean": statistics.mean(latencies),
                    "max": max(latencies),
                },
                "roundtrips_per_second": rounds / elapsed,
                "rss_initial_bytes": initial_rss,
                "rss_final_bytes": rss_bytes(),
                "postgres_public_relation_growth_bytes": int(after - before),
                "journal_count": journal_count,
                "ledger_entry_count": ledger_count,
                "reconciliation": safe_json(reconciliation),
                "valuation": valuation,
                "interpretation": "Synthetic isolated SQL transactions; two leg commits independent; no live opportunity or trading claim",
            }
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", default="120,200,300,500")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--worker", type=int)
    parser.add_argument("--evidence-worker", choices=["before", "after"])
    parser.add_argument("--evidence-routes", type=int, default=500)
    parser.add_argument("--evidence-only", action="store_true")
    parser.add_argument("--paper-ledger", action="store_true")
    parser.add_argument("--paper-rounds", type=int, default=100)
    args = parser.parse_args()
    if not 2 <= args.seconds <= 3600:
        parser.error("--seconds must be2–3600")
    if args.paper_ledger:
        print(
            json.dumps(
                asyncio.run(paper_ledger_measurement(args.paper_rounds)), indent=2
            )
        )
        return
    if args.worker:
        print(json.dumps(asyncio.run(scanner_measurement(args.worker, args.seconds))))
        return
    if args.evidence_worker:
        print(
            json.dumps(
                evidence_measurement(
                    args.evidence_routes, args.evidence_worker == "after"
                )
            )
        )
        return
    output = {
        "schema_version": "stage4-capacity-v1",
        "kind": "EXPLICITLY_SYNTHETIC_OFFLINE_CAPACITY",
        "no_external_exchange_traffic": True,
        "evidence_optimization": [],
        "capacity": [],
    }
    for variant in ("before", "after"):
        result = subprocess.run(
            [
                sys.executable,
                __file__,
                "--evidence-worker",
                variant,
                "--evidence-routes",
                str(args.evidence_routes),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        output["evidence_optimization"].append(json.loads(result.stdout))
    if not args.evidence_only:
        for count in [int(x) for x in args.counts.split(",")]:
            result = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--worker",
                    str(count),
                    "--seconds",
                    str(args.seconds),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            output["capacity"].append(json.loads(result.stdout))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
