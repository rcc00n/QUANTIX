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

    def market_status(self):
        now = self.utcnow()
        clock = self.utc_clock.snapshot() if self.utc_clock else {"trusted": False}
        items = {}
        for slug, rt in self.runtimes.items():
            books = [self.cache.get(m.id, now=now) for m in rt.markets]
            available = [b for b in books if b is not None]
            classes = Counter(
                freshness(b, now, self.config, clock)["classification"] for b in available
            )
            classes["UNAVAILABLE"] += len(books) - len(available)
            sources = [b.exchange_timestamp for b in available if b.exchange_timestamp is not None]
            receive = max((b.received_at for b in available), default=None)
            state = "UNAVAILABLE" if not available else "DEGRADED"
            if (
                available
                and all(b.state == DataState.HEALTHY for b in available)
                and not rt.health["error_code"]
            ):
                state = "HEALTHY"
            if available and classes["STALE"] == len(available):
                state = "STALE"
            elif classes["STALE"] and state == "HEALTHY":
                state = "DEGRADED"
            if not rt.markets:
                state = "UNAVAILABLE"
            reasons = Counter(reason for b in available for reason in b.reasons)
            if not clock.get("trusted"):
                reasons["UTC_CLOCK_UNTRUSTED"] += 1
                if state == "HEALTHY":
                    state = "DEGRADED"
            items[slug] = {
                **safe_json(rt.health),
                "state": state,
                "status_generated_at": now.isoformat(),
                "last_status_check_at": now.isoformat(),
                "last_collector_received_at": receive.isoformat() if receive else None,
                "last_source_update_at": max(sources).isoformat() if sources else None,
                "freshness_seconds": age_seconds(now, receive) if clock.get("trusted") else None,
                "book_age_seconds": distribution(age_seconds(now, b.received_at) for b in available)
                if clock.get("trusted")
                else distribution([]),
                "freshness_counts": dict(classes),
                "quality_reasons": dict(reasons),
                "timings": rt.timings.snapshot(),
                "queue_backlog": sum(s.due <= time.monotonic() for s in rt.schedules.values()),
                "rate_limit_state": "BACKOFF" if rt.cooldown_until > time.monotonic() else "READY",
                "ws_last_message_at": rt.ws_last_message_at.isoformat()
                if rt.ws_last_message_at
                else None,
                "ws_synchronized": rt.ws_synchronized,
                "native_stream": self.native_status(rt, now, clock.get("trusted")),
                "transport_state": "HEALTHY"
                if rt.ws_synchronized
                and rt.ws_last_message_elapsed is not None
                and elapsed_seconds() - rt.ws_last_message_elapsed
                <= self.config.websocket_idle_timeout_seconds
                else "REST"
                if not rt.health["error_code"]
                else "DEGRADED",
                "clock_trusted": bool(clock.get("trusted")),
                "source": "live_collector",
                "active_monitored_markets": [m.native_symbol for m in rt.markets],
            }
        return {
            "items": items,
            "clock": clock,
            "run_id": str(self.store.run_id),
            "healthy": self.last_heartbeat > 0 and time.monotonic() - self.last_heartbeat < 30,
            "status_generated_at": now.isoformat(),
        }

    def monitoring_allocation(self):
        relevance = {}
        market_by_id = {str(m.id): m for rt in self.runtimes.values() for m in rt.markets}
        if self.scanner:
            for route in self.scanner.current.values():
                if route["status"] in {"RESEARCH_ONLY", "VERIFIED_MARKET", "RECHECKING"}:
                    inputs = route.get("evidence", {}).get("calculation_inputs", {})
                    for side in ("buy", "sell"):
                        market = market_by_id.get(inputs.get(side, {}).get("market_id"))
                        if market:
                            key = (market.base.key, market.quote.key)
                            relevance[key] = relevance.get(key, 0) + 1
        quality = {
            (book.exchange, book.symbol): self.cache.get(book.market_id).state.value
            for book in self.cache.books.values()
        }
        catalogs = {slug: rt.catalog for slug, rt in self.runtimes.items()}
        if self.terminal_market_preferences:
            allowed = self.terminal_market_preferences["enabled_exchanges"]
            catalogs = {
                slug: rows if not allowed or slug in allowed else []
                for slug, rows in catalogs.items()
            }
        return allocate(
            catalogs,
            self.config,
            relevance,
            quality,
        )

    async def reload_terminal_preferences(self):
        from arbiq.db.models import User
        from arbiq.settings.terminal import read_effective_terminal

        async with self.store.sessions() as db:
            owner = await db.scalar(
                select(User.id).where(User.username == self.settings.admin_username)
            )
            if owner is None:
                return
            row, settings = await read_effective_terminal(db, owner)
        if not row or row.version == self.applied_terminal_version:
            return  # Environment defaults remain effective until an actual owner save.
        prefs = settings.market
        self.config = CollectorConfig.model_validate(
            {
                **self.settings.collector.model_dump(),
                "base_assets": list(prefs.enabled_assets),
                "quote_assets": list(prefs.preferred_quote_currencies),
                "prefer_cad": prefs.cad_focused,
                "maximum_age_seconds": prefs.maximum_age_seconds,
                "minimum_quote_volume": prefs.minimum_liquidity,
                "max_markets_per_exchange": prefs.maximum_markets_per_exchange,
            }
        )
        self.cache.config = self.config
        self.cache.limit = len(self.runtimes) * self.config.max_markets_per_exchange
        if self.scanner:
            self.scanner.config = self.scanner.config.model_copy(
                update={
                    "maximum_book_age_seconds": prefs.maximum_age_seconds,
                }
            )
        self.terminal_market_preferences = prefs.model_dump(mode="json")
        self.applied_terminal_version = row.version
        await self.store.event(
            None,
            "collector.settings_applied",
            "Owner market coverage settings applied",
            details={"version": row.version, "minimum_liquidity_unit": "native quote volume"},
        )

    def apply_exchange_cooldown(self, runtime, code: str, scheduled_until: float = 0):
        if code in {"RATE_LIMIT", "ACCESS_DENIED", "PUBLIC_ENDPOINT_REQUIRES_AUTH"}:
            floor = max(
                getattr(runtime.connector, "retry_after_seconds", 0),
                300 if code != "RATE_LIMIT" else 30,
            )
            runtime.cooldown_until = max(
                runtime.cooldown_until, scheduled_until, time.monotonic() + floor
            )

    async def discover(self, runtime: ExchangeRuntime):
        # Keep a venue's native full catalog stable until normalization/persistence
        # and focus publish finish. Other venues continue independently.
        async with runtime.metadata_lock:
            await self._discover_locked(runtime)

    async def _discover_locked(self, runtime: ExchangeRuntime):
        raw_markets = await runtime.connector.discover_spot_markets()
        tickers = {}
        # Restrict bulk ticker calls to known single-request public endpoints.
        if runtime.slug in {
            "kraken",
            "ndax",
            "cryptocom",
            "bitstamp",
            "binance",
            "bybit",
            "okx",
            "mexc",
            "bitget",
            "htx",
            "poloniex",
        }:
            try:
                tickers = await runtime.connector.fetch_tickers()
            except Exception as exc:
                self.apply_exchange_cooldown(runtime, classify_error(exc))
                await self.store.event(
                    runtime.exchange_id,
                    "collector.liquidity_metadata_unavailable",
                    "Optional ticker liquidity unavailable; slower book scheduling retained",
                    "warning",
                    {"error_code": classify_error(exc)},
                )
        markets = []
        identity_by_symbol = {}
        malformed = 0
        for raw in raw_markets:
            try:
                markets.append(
                    normalize_market(
                        runtime.slug,
                        raw,
                        runtime.connector.adapter.currencies,
                        tickers.get(raw.get("symbol"), {}),
                        runtime.connector.adapter.precisionMode,
                        identity_by_symbol=identity_by_symbol,
                    )
                )
            except (ValueError, TypeError, KeyError, ArithmeticError):
                malformed += 1
        if not markets:
            raise ValueError("No valid spot metadata discovered")
        caps = capabilities(runtime.connector.adapter, raw_markets)
        # The native tables are still needed for archive capture; the extra spot
        # list and one-shot ticker map are not needed across database/focus awaits.
        del raw_markets, tickers, identity_by_symbol
        runtime.exchange_id = await self.store.register_exchange(
            runtime.slug, runtime.connector.adapter.name, caps
        )
        runtime.catalog = markets
        selected = self.monitoring_allocation().get(runtime.slug, [])
        chosen = await self.store.save_markets(runtime.exchange_id, markets, selected)
        if hasattr(runtime.connector, "archive_metadata"):
            try:
                await runtime.connector.archive_metadata()
            except (ValueError, TypeError):
                # An unfamiliar native cache shape must not disable a working
                # market feed or discard any discovery/financial evidence.
                await self.store.event(
                    runtime.exchange_id,
                    "collector.metadata_cache_unoptimized",
                    "Native metadata retained expanded; cache compaction unavailable",
                    "warning",
                )
        await self._apply_selection_locked(runtime, chosen)

    async def apply_selection(self, runtime, chosen):
        async with runtime.metadata_lock:
            await self._apply_selection_locked(runtime, chosen)

    async def _apply_selection_locked(self, runtime, chosen):
        selected_ids = {market.id for market in chosen}
        missing = selected_ids - self.cache.books.keys() - self.cache.watermarks.keys()
        watermarks = None
        if missing and hasattr(self.store, "latest_watermarks"):
            watermarks = await self.store.latest_watermarks(missing)
        if hasattr(runtime.connector, "focus_metadata"):
            await runtime.connector.focus_metadata([market.native_symbol for market in chosen])

        # The exchange loop reads these together. Keep the prior selection until
        # async preparation finishes, then install all runtime state without an await.
        previous = set(runtime.schedules)
        if isinstance(watermarks, dict):
            self.cache.seed_watermarks(watermarks)
        runtime.schedules = {
            market.id: runtime.schedules.get(market.id, Schedule()) for market in chosen
        }
        runtime.markets = chosen
        runtime.confirmation.intersection_update(selected_ids)
        monitored = self.monitored_ids()
        for key in list(self.confirmation_enqueued):
            if key not in monitored:
                self.confirmation_enqueued.pop(key, None)
        for mapping in (runtime.ws_last, runtime.ws_published, runtime.last_confirmation):
            for key in list(mapping):
                if key not in selected_ids:
                    mapping.pop(key)
        self.cache.retain(monitored)
        self.store.retain_runtime_keys(monitored)
        runtime.discovery.success(self.config.discovery_interval_seconds)
        now = self.utcnow()
        runtime.health.update(
            market_count=len(runtime.catalog),
            cad_market_count=sum(m.quote.symbol == "CAD" for m in runtime.catalog),
            monitored_count=len(chosen),
            last_discovery_at=now,
            updated_at=now,
            error_code=None,
            state="DEGRADED",
            consecutive_failures=0,
            next_retry_at=None,
        )
        await self.store.event(
            runtime.exchange_id,
            "exchange.markets_discovered",
            "Spot markets dynamically discovered",
            details={
                "spot_markets": len(runtime.catalog),
                "monitored": len(chosen),
                "allocation": "cross_venue_intersections",
            },
        )
        if not chosen:
            runtime.health["error_code"] = "NO_MARKETS_MATCH_FILTERS"
        if (
            runtime.slug == "kraken"
            and runtime.slug in self.config.websocket_exchanges
            and set(runtime.schedules) != previous
        ):
            if runtime.ws_task:
                runtime.ws_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runtime.ws_task
            if chosen:
                by_symbol = {m.native_symbol: m for m in chosen}

                async def on_book(symbol, raw):
                    market = by_symbol[symbol]
                    if market.id not in runtime.schedules:
                        return
                    now_mono = time.monotonic()
                    runtime.ws_last[market.id] = now_mono
                    if (
                        now_mono - runtime.ws_published.get(market.id, -1000)
                        < self.config.websocket_publish_interval_seconds
                    ):
                        return
                    runtime.ws_published[market.id] = now_mono
                    now = self.utcnow()
                    received = raw.get("_collector_received_at") or now
                    await self.accept(runtime, market, raw, received, received, None, "kraken_ws")
                    runtime.health["connection_mode"] = "WebSocket + REST"

                async def on_error(code, reconnects):
                    runtime.health.update(connection_mode="REST fallback", reconnects=reconnects)
                    runtime.ws_last.clear()
                    runtime.ws_synchronized = False
                    await self.store.event(
                        runtime.exchange_id,
                        "exchange.websocket_resync",
                        "WebSocket stream requires fresh snapshot; REST fallback active",
                        "warning",
                        {"error_code": code},
                    )

                async def on_message(synchronized):
                    runtime.ws_last_message_at = self.utcnow()
                    runtime.ws_last_message_elapsed = elapsed_seconds()
                    runtime.ws_synchronized = synchronized

                stream = KrakenStream(
                    self.config, on_book, on_error, on_message=on_message, utcnow=self.utcnow
                )
                runtime.ws_task = asyncio.create_task(stream.run(list(by_symbol), self.stop))
        if runtime.slug in NATIVE_PROTOCOLS:
            await self.configure_native(runtime)

