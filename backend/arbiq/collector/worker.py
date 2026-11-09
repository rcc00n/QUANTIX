"""Single-owner, bounded public data worker. Network failures isolate per exchange."""

import asyncio
import contextlib
import signal
import socket
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from uuid import UUID, uuid4

import aiohttp
import structlog
from aiohttp import web
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import ClockGuard, Schedule
from arbiq.collector.storage import CollectorStore
from arbiq.collector.stream_selection import native_symbols, tier
from arbiq.connectors.ccxt_public import PublicConnector, classify_error
from arbiq.connectors.coinbase_ws import CoinbaseProtocol
from arbiq.connectors.kraken_ws import KrakenStream
from arbiq.connectors.native_stream import NativeStream
from arbiq.connectors.okx_ws import OkxProtocol
from arbiq.connectors.registry import available_candidates, capabilities, eligibility
from arbiq.core.clock import TrustedClock, elapsed_seconds
from arbiq.core.config import Settings, get_settings
from arbiq.core.logging import configure_logging
from arbiq.core.memory import reclaim_unused_pages
from arbiq.core.telemetry import ProcessTelemetry
from arbiq.db.schema import EXPECTED_SCHEMA_REVISION
from arbiq.finance.contracts import exact_decimal
from arbiq.market_data.cache import BookCache
from arbiq.market_data.discovery import DiscoveredMarket, normalize_market
from arbiq.market_data.quality import DataState, safe_json, validate_book
from arbiq.market_data.timing import Timings, age_seconds, distribution, freshness
from arbiq.opportunities.coverage import allocate
from arbiq.opportunities.engine import ArbitrageEngine
from arbiq.paper_trading.coordinator import PaperCoordinator

log = structlog.get_logger()
NATIVE_PROTOCOLS = {"coinbaseexchange": CoinbaseProtocol, "okx": OkxProtocol}


@dataclass
class ExchangeRuntime:
    slug: str
    connector: PublicConnector
    exchange_id: UUID | None = None
    markets: list[DiscoveredMarket] = field(default_factory=list)
    catalog: list[DiscoveredMarket] = field(default_factory=list)
    schedules: dict[UUID, Schedule] = field(default_factory=dict)
    discovery: Schedule = field(default_factory=Schedule)
    health: dict = field(
        default_factory=lambda: {
            "state": "UNAVAILABLE",
            "connection_mode": "REST",
            "market_count": 0,
            "cad_market_count": 0,
            "monitored_count": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "reconnects": 0,
            "consecutive_failures": 0,
            "latency_ms": None,
            "last_success_at": None,
            "last_attempt_at": None,
            "last_discovery_at": None,
            "next_retry_at": None,
            "error_code": None,
            "updated_at": datetime.now(UTC),
        }
    )
    cooldown_until: float = 0.0
    ws_task: asyncio.Task | None = None
    ws_last: dict[UUID, float] = field(default_factory=dict)
    ws_published: dict[UUID, float] = field(default_factory=dict)
    confirmation: set[UUID] = field(default_factory=set)
    last_confirmation: dict[UUID, float] = field(default_factory=dict)
    metadata_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timings: Timings = field(default_factory=Timings)
    ws_last_message_at: datetime | None = None
    ws_synchronized: bool = False
    ws_last_message_elapsed: float | None = None
    native_stream: NativeStream | None = None
    native_symbols: dict[str, DiscoveredMarket] = field(default_factory=dict)
    native_synchronized: frozenset[UUID] = frozenset()
    native_generation: int = 0
    native_integrity_epoch: int = 0
    native_last_error: str | None = None
    transport_counts: Counter = field(default_factory=Counter)


class Collector:
    def __init__(self, settings: Settings, store: CollectorStore):
        self.settings = settings
        self.config: CollectorConfig = settings.collector
        self.store = store
        self.stop = asyncio.Event()
        self.gate = asyncio.Semaphore(self.config.max_concurrency)
        self.metadata_gate = asyncio.Semaphore(max(1, min(2, self.config.max_concurrency - 1)))
        self.cache = BookCache(self.config)
        self.clock = ClockGuard(self.config.maximum_future_skew_seconds)
        self.runtimes: dict[str, ExchangeRuntime] = {}
        self.tasks: list[asyncio.Task] = []
        self.last_heartbeat = 0.0
        self.ingestion_index = 0
        self.confirmation_queue = asyncio.Queue(maxsize=32)
        self.utc_clock = None
        self.scanner = None
        self.paper = None
        self.allocation_lock = asyncio.Lock()
        self.telemetry = ProcessTelemetry()
        self.api_latencies = deque(maxlen=600)
        self.applied_terminal_version = 0
        self.terminal_market_preferences = None
        self.memory_maintenance = None
        self.timings = Timings()
        self.confirmation_enqueued = {}

    def utcnow(self):
        return self.utc_clock.utcnow() if self.utc_clock else datetime.now(UTC)

    async def synchronize_clock(self):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=4)) as client:
            while not self.stop.is_set():
                await self.utc_clock.synchronize(client)
                try:
                    await asyncio.wait_for(self.stop.wait(), 30)
                except TimeoutError:
                    pass

    def monitored_ids(self):
        return {market.id for rt in self.runtimes.values() for market in rt.markets}

    def native_status(self, runtime, now, clock_trusted):
        stream = runtime.native_stream
        fast = {m.id for m in runtime.native_symbols.values()} | set(runtime.ws_last)
        books = [self.cache.get(m.id, now=now) for m in runtime.native_symbols.values()]
        streamed = [b for b in books if b is not None and b.source == f"{runtime.slug}_ws"]
        return {
            "subscribed": sorted(runtime.native_symbols),
            "synchronized": len(runtime.native_synchronized),
            "cached_stream_books": len(streamed),
            "valid_cached_stream_books": sum(
                b.market_id in runtime.native_synchronized
                and b.state in {DataState.HEALTHY, DataState.DEGRADED}
                for b in streamed
            ),
            "book_age_seconds": distribution(
                age_seconds(now, b.received_at) for b in streamed if clock_trusted
            ),
            "last_error": runtime.native_last_error,
            "counters": {**(stream.counts if stream else {}), **runtime.transport_counts},
            "market_tiers": {
                m.native_symbol: tier(m, self.config.priority_assets, fast) for m in runtime.markets
            },
        }

