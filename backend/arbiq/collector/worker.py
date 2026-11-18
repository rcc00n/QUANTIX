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
from arbiq.collector.reliability import (
    CHECK_LABELS,
    CollectorReliability,
    clock_trust,
    market_coverage,
    native_streams,
    scanner_progress,
)
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
from arbiq.opportunities.coverage_analytics import asset_coverage
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
        self.allocation_tick = 0
        self.allocation_quality = {}
        self.allocation_depth = {}
        self.memory_maintenance = None
        self.timings = Timings()
        self.confirmation_enqueued = {}
        self.reliability = CollectorReliability()

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

    async def sample_reliability(self, now: datetime, clock: dict, engine: dict) -> None:
        stamp = elapsed_seconds()
        counts = Counter()
        for market_id in self.monitored_ids():
            book = self.cache.books.get(market_id)
            state = (
                "UNAVAILABLE"
                if book is None or book.state in {DataState.UNAVAILABLE, DataState.SUSPICIOUS}
                else freshness(book, now, self.config, clock)["classification"]
            )
            counts[state] += 1
        subscriptions, synchronized = {}, {}
        configured_streams = set(self.config.websocket_exchanges)
        for slug, runtime in self.runtimes.items():
            if slug not in configured_streams:
                continue
            requested = len(runtime.markets) if slug == "kraken" else len(runtime.native_symbols)
            if requested == 0:
                continue
            subscriptions[slug] = requested
            last_message = runtime.ws_last_message_elapsed
            if (
                runtime.ws_task is None
                or runtime.ws_task.done()
                or last_message is None
                or not 0 <= stamp - last_message <= self.config.websocket_idle_timeout_seconds
            ):
                continue
            if slug == "kraken":
                synchronized[slug] = requested if runtime.ws_synchronized else 0
            else:
                synchronized[slug] = sum(
                    market.id in runtime.native_synchronized
                    for market in runtime.native_symbols.values()
                )
        scanner = self.scanner
        scanner_config = scanner.config if scanner is not None else self.settings.arbitrage
        heartbeat_age = (
            time.monotonic() - scanner.heartbeat if scanner and scanner.heartbeat > 0 else None
        )
        metrics = engine.get("metrics", {})
        trusted = clock.get("trusted") is True
        transitions = self.reliability.observe(
            stamp,
            now if trusted else None,
            {
                "market_coverage": market_coverage(counts, clock_trusted=trusted),
                "scanner_progress": scanner_progress(
                    enabled=scanner_config.enabled,
                    ready=metrics.get("pending_comparisons", 0),
                    capacity=scanner_config.max_candidates * 4,
                    oldest=max(
                        metrics.get("comparison_queue_oldest_seconds", 0),
                        metrics.get("comparison_deferred_oldest_seconds", 0),
                    ),
                    heartbeat_age=heartbeat_age,
                    deferred=metrics.get("comparison_deferred_markets", 0),
                    stale_seconds=min(
                        self.config.freshness_stale_seconds,
                        self.config.maximum_age_seconds,
                        scanner_config.maximum_book_age_seconds,
                    ),
                ),
                "native_streams": native_streams(
                    enabled="kraken" in configured_streams
                    or (
                        self.config.websocket_fast_markets_per_exchange > 0
                        and bool(NATIVE_PROTOCOLS.keys() & configured_streams)
                    ),
                    subscriptions=subscriptions,
                    synchronized=synchronized,
                ),
                "clock_trust": clock_trust(clock),
            },
        )
        for transition in transitions:
            degraded = transition.state == "DEGRADED"
            suffix = "degraded" if degraded else "recovered"
            await self.store.event(
                None,
                f"collector.reliability_{suffix}",
                f"{CHECK_LABELS[transition.key]} {suffix}",
                "warning" if degraded else "info",
                {
                    "check": transition.key,
                    "state": transition.state,
                    "reason": transition.reason,
                    "collector_run_id": str(self.store.run_id),
                    "observed_at": now.isoformat() if trusted else None,
                },
            )

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
        profile = self.config.market_profile
        now = self.utcnow()
        market_by_id = {str(m.id): m for rt in self.runtimes.values() for m in rt.markets}
        if self.scanner:
            for route in self.scanner.current.values():
                if route["status"] in {"RESEARCH_ONLY", "VERIFIED_MARKET", "RECHECKING"}:
                    if profile is not None:
                        profit = route.get("net_profit_cad")
                        evidence = route.get("evidence", {})
                        if (
                            profit is None
                            or Decimal(profit) <= 0
                            or evidence.get("buy_quality") != "HEALTHY"
                            or evidence.get("sell_quality") != "HEALTHY"
                            or not evidence.get("timing", {}).get("clock_trusted")
                        ):
                            continue
                    inputs = route.get("evidence", {}).get("calculation_inputs", {})
                    for side in ("buy", "sell"):
                        market = market_by_id.get(inputs.get(side, {}).get("market_id"))
                        if market:
                            key = (market.base.key, market.quote.key)
                            relevance[key] = relevance.get(key, 0) + 1
        quality, depth = {}, {}
        for market_id in self.cache.books:
            book = self.cache.get(market_id, now=now)
            key = (book.exchange, book.symbol)
            quality[key] = book.state.value
            if profile is not None and book.state == DataState.HEALTHY:
                depth[key] = min(
                    sum(level.price * level.quantity for level in levels)
                    for levels in (book.normalized.asks, book.normalized.bids)
                )
        self.allocation_quality, self.allocation_depth = quality, depth
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
            research_epoch=self.allocation_tick // 10,
            depth=depth,
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
                "market_profile": prefs.market_profile or self.settings.collector.market_profile,
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
        rules_observed_at = self.utcnow() if self.utc_clock and self.utc_clock.trusted else None
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
                        rules_observed_at=rules_observed_at,
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
            runtime.ws_task = None
            runtime.ws_synchronized = False
            runtime.ws_last_message_elapsed = None
            runtime.ws_last_message_at = None
            runtime.ws_last.clear()
            runtime.ws_published.clear()
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

    def retire_native(self, runtime, *, fallback=True):
        runtime.native_integrity_epoch += 1
        runtime.native_synchronized = frozenset()
        runtime.ws_synchronized = False
        runtime.ws_last_message_elapsed = None
        now = time.monotonic()
        source = f"{runtime.slug}_ws"
        for offset, market in enumerate(runtime.native_symbols.values()):
            market_id = market.id
            book = self.cache.books.get(market_id)
            if book and book.source == source:
                revoked = book.classified(
                    DataState.STALE,
                    reasons=tuple(dict.fromkeys((*book.reasons, "stream_unsynchronized"))),
                )
                self.cache.books[market_id] = revoked
                if self.scanner:
                    self.scanner.changed(revoked)
            schedule = runtime.schedules.get(market_id)
            if fallback and runtime.ws_last.pop(market_id, None) is not None and schedule:
                if schedule.failures == 0 and schedule.due > now:
                    schedule.due = max(
                        runtime.cooldown_until,
                        min(
                            schedule.due,
                            now + offset * self.config.minimum_request_interval_seconds,
                        ),
                    )
        runtime.ws_last.clear()

    async def configure_native(self, runtime):
        selected = native_symbols(
            runtime.slug,
            {slug: other.markets for slug, other in self.runtimes.items()},
            self.config.priority_assets,
            self.config.websocket_fast_markets_per_exchange
            if runtime.slug in self.config.websocket_exchanges
            else 0,
            profile=self.config.market_profile,
            quality=self.allocation_quality,
            depth=self.allocation_depth,
            retained=runtime.native_symbols,
        )
        if {key: m.id for key, m in selected.items()} == {
            key: m.id for key, m in runtime.native_symbols.items()
        }:
            return
        runtime.native_generation += 1
        generation = runtime.native_generation
        self.retire_native(runtime)
        if runtime.ws_task:
            runtime.ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.ws_task
        runtime.ws_task = runtime.native_stream = None
        runtime.native_symbols = selected
        runtime.native_last_error = None
        runtime.health["connection_mode"] = "REST fallback" if selected else "REST"
        if not selected:
            return

        async def on_book(symbol, raw):
            market = next((m for m in runtime.markets if m.id == selected[symbol].id), None)
            if (
                generation != runtime.native_generation
                or market is None
                or market.id not in runtime.native_synchronized
            ):
                return
            raw["symbol"] = market.native_symbol
            raw["stream_integrity"]["collector_integrity_epoch"] = runtime.native_integrity_epoch
            received = raw["_collector_received_at"]
            book = await self.accept(
                runtime, market, raw, received, received, None, f"{runtime.slug}_ws"
            )
            if (
                generation == runtime.native_generation
                and market.id in runtime.native_synchronized
                and book is not None
                and book.state in {DataState.HEALTHY, DataState.DEGRADED}
                and book.exchange_timestamp is not None
            ):
                runtime.ws_last[market.id] = time.monotonic()
                runtime.health["connection_mode"] = "WebSocket + REST"

        async def on_error(code, reconnects):
            if generation != runtime.native_generation:
                return
            self.retire_native(runtime)
            runtime.native_last_error = code
            runtime.health.update(connection_mode="REST fallback", reconnects=reconnects)
            await self.store.event(
                runtime.exchange_id,
                "exchange.websocket_resync",
                "Native depth invalidated, REST fallback active",
                "warning",
                {"error_code": code, "source": f"{runtime.slug}_ws"},
            )

        async def on_message(synchronized, received, received_elapsed):
            if generation == runtime.native_generation:
                runtime.native_synchronized = frozenset(selected[key].id for key in synchronized)
                runtime.ws_synchronized = len(synchronized) == len(selected)
                if runtime.ws_synchronized:
                    runtime.native_last_error = None
                runtime.ws_last_message_at = received
                runtime.ws_last_message_elapsed = received_elapsed

        runtime.native_stream = NativeStream(
            self.config,
            NATIVE_PROTOCOLS[runtime.slug],
            on_book,
            on_error,
            on_message,
            self.utcnow,
            clock_trusted=lambda: self.utc_clock is not None and self.utc_clock.trusted,
        )
        runtime.ws_task = asyncio.create_task(runtime.native_stream.run(list(selected), self.stop))

    def native_book_fresh(self, runtime, market_id, now):
        if (
            not self.utc_clock
            or not self.utc_clock.trusted
            or market_id not in runtime.native_synchronized
            or runtime.ws_last_message_elapsed is None
            or elapsed_seconds() - runtime.ws_last_message_elapsed
            > self.config.websocket_idle_timeout_seconds
        ):
            return False
        book = self.cache.get(market_id, now=now)
        if (
            not book
            or book.source != f"{runtime.slug}_ws"
            or book.state not in {DataState.HEALTHY, DataState.DEGRADED}
            or book.exchange_timestamp is None
        ):
            return False
        # Quiet markets retain their quote ages and receive a paced REST
        # verification before the owner's age limit. Heartbeats cannot renew them.
        age = (now - min(book.received_at, book.exchange_timestamp)).total_seconds()
        return (
            0 <= age < min(self.config.freshness_stale_seconds, self.config.maximum_age_seconds / 2)
        )

    async def accept(
        self, runtime, market, raw, received, started, latency, source, confirmation=False
    ):
        processing_started = elapsed_seconds()
        selection = runtime.schedules.get(market.id)
        if self.terminal_market_preferences:
            allowed = self.terminal_market_preferences["enabled_exchanges"]
            if (allowed and runtime.slug not in allowed) or market.id not in runtime.schedules:
                await self.store.event(
                    runtime.exchange_id,
                    "collector.late_deselected_response",
                    "Late response arrived after this market was deselected",
                    details={"market_id": str(market.id), "source": source},
                )
                return
        if latency is not None and source == "rest" and 0 <= latency < 300000:
            self.api_latencies.append(latency)
        book = validate_book(
            raw,
            market_id=market.id,
            exchange=runtime.slug,
            symbol=market.native_symbol,
            base_key=market.base.key,
            quote_key=market.quote.key,
            received_at=received,
            request_started_at=started,
            latency_ms=latency,
            config=self.config,
            source=source,
            identity_verified=market.base.verified and market.quote.verified,
            active_verified=market.active is True,
            clock_healthy=self.utc_clock.trusted if self.utc_clock else self.clock.healthy,
        )
        self.ingestion_index += 1
        pipeline = {
            **(getattr(runtime.connector, "last_timing", {}) if source == "rest" else {}),
            "collector_received_at": received.isoformat(),
            "collector_processed_at": self.utcnow().isoformat(),
            "validation_ms": (elapsed_seconds() - processing_started) * 1000,
            "stream_parse_ms": raw.get("_stream_parse_ms") if isinstance(raw, dict) else None,
            "stream_queue_ms": raw.get("_stream_queue_ms") if isinstance(raw, dict) else None,
        }
        book = book.model_copy(update={"ingestion_index": self.ingestion_index}).with_pipeline(
            **pipeline
        )
        if self.utc_clock:
            book = book.model_copy(
                update={"verification": {**book.verification, "clock": self.utc_clock.snapshot()}}
            )
        rejection = self.cache.rejection_reason(book)
        candidate = book
        book = self.cache.put(book, confirmation=confirmation)
        if rejection:
            await self.store.event(
                runtime.exchange_id,
                "collector.update_rejected",
                "Observation retained as rejected evidence; current cache preserved",
                "warning",
                {
                    "reason": rejection,
                    "market_id": str(market.id),
                    "rejected": candidate.model_dump(mode="json"),
                },
            )
            return None
        book = book.with_pipeline(cache_updated_at=self.utcnow().isoformat())
        self.cache.books[book.market_id] = book
        # Publish the changed immutable cache observation before a throttled DB write.
        if self.scanner:
            self.scanner.changed(book)
        persistence_started = elapsed_seconds()
        persisted = await self.store.save_book(book, force=confirmation)
        persistence_ms = (elapsed_seconds() - persistence_started) * 1000
        if persisted is True:
            self.timings.add("persistence_commit_ms", persistence_ms)
            if self.cache.books.get(book.market_id) is book:
                book = book.with_pipeline(
                    persisted_at=self.utcnow().isoformat(), persistence_commit_ms=persistence_ms
                )
                self.cache.books[book.market_id] = book
        for name in (
            "validation_ms",
            "request_ms",
            "http_body_ms",
            "adapter_parse_ms",
            "pacing_wait_ms",
            "connector_queue_ms",
            "global_queue_ms",
            "stream_parse_ms",
            "stream_queue_ms",
        ):
            runtime.timings.add(name, pipeline.get(name))
        if source.endswith("_ws") and book.exchange_timestamp and self.utc_clock:
            if self.utc_clock.trusted:
                runtime.timings.add(
                    "stream_source_receive_ms",
                    (received - book.exchange_timestamp).total_seconds() * 1000,
                )
        if self.paper:
            await self.paper.observe(book)
        if selection is not None and runtime.schedules.get(market.id) is not selection:
            # The already accepted observation remains historical evidence, while
            # retired work must not overwrite the current connector status.
            return book
        structurally_available = bool(book.normalized.bids and book.normalized.asks) and not any(
            reason.startswith("invalid_")
            or reason.startswith("unordered_")
            or reason == "symbol_mismatch"
            for reason in book.reasons
        )
        if structurally_available:
            runtime.health.update(
                last_success_at=max(received, runtime.health["last_success_at"] or received),
                updated_at=self.utcnow(),
                error_code=None,
                consecutive_failures=0,
                next_retry_at=None,
            )
        else:
            runtime.health.update(updated_at=received, error_code="INVALID_BOOK")
            runtime.health["failed_requests"] += 1
        runtime.health["successful_requests"] += 1
        if latency is not None:
            runtime.health["latency_ms"] = latency
        if book.state == DataState.SUSPICIOUS:
            # Automatically request a fresh REST recheck, bounded and deduplicated.
            self.request_confirmation(market.id)
        return book

    def request_confirmation(self, market_id: UUID) -> str:
        runtime = next((rt for rt in self.runtimes.values() if market_id in rt.schedules), None)
        if runtime is None:
            return "NOT_MONITORED"
        now = time.monotonic()
        if (
            market_id in runtime.confirmation
            or now - runtime.last_confirmation.get(market_id, -1000) < 30
        ):
            return "COOLDOWN"
        if (
            self.confirmation_queue.full()
            or len(self.confirmation_enqueued) >= self.confirmation_queue.maxsize
        ):
            return "QUEUE_FULL"
        runtime.last_confirmation[market_id] = now
        runtime.confirmation.add(market_id)
        self.confirmation_queue.put_nowait((runtime.slug, market_id))
        self.confirmation_enqueued[market_id] = elapsed_seconds()
        if runtime.schedules[market_id].failures == 0:
            runtime.schedules[market_id].due = 0
        return "QUEUED"

    async def fetch(self, runtime, market):
        schedule = runtime.schedules.get(market.id)
        if schedule is None:
            return
        if time.monotonic() < runtime.cooldown_until:
            schedule.due = max(schedule.due, runtime.cooldown_until)
            return
        confirmation = market.id in runtime.confirmation
        runtime.transport_counts["rest_attempts"] += 1
        if market.id in {m.id for m in runtime.native_symbols.values()}:
            runtime.transport_counts["rest_on_stream_market"] += 1
        request_start_mono = time.monotonic()
        runtime.timings.add(
            "scheduling_lag_ms",
            max(0, time.monotonic() - schedule.due) * 1000 if schedule.due else None,
        )
        started = self.utcnow()
        runtime.health["last_attempt_at"] = started
        try:
            # Native facade acquires the shared gate after pacing; generic adapters
            # still receive the same hard concurrency bound at this boundary.
            gate = (
                contextlib.nullcontext()
                if getattr(runtime.connector, "_request_gate", None) is self.gate
                else self.gate
            )
            async with gate:
                raw = await runtime.connector.fetch_raw_book(market.native_symbol)
            received = self.utcnow()
            if runtime.schedules.get(market.id) is not schedule:
                await self.store.event(
                    runtime.exchange_id,
                    "collector.late_deselected_response",
                    "Late response arrived after this market was deselected",
                    details={"market_id": str(market.id), "source": "rest"},
                )
                return
            current = self.cache.books.get(market.id)
            if (
                not confirmation
                and current is not None
                and current.received_at >= started
                and self.native_book_fresh(runtime, market.id, received)
            ):
                runtime.transport_counts["late_rest_superseded"] += 1
            else:
                await self.accept(
                    runtime,
                    market,
                    raw,
                    received,
                    started,
                    runtime.connector.last_latency_ms,
                    "rest",
                    confirmation,
                )
            liquid = market.quote_volume is not None and market.quote_volume >= Decimal("1000000")
            interval = (
                self.config.fast_interval_seconds if liquid else self.config.slow_interval_seconds
            )
            if runtime.slug in {"bitstamp", "coinbaseexchange"}:
                interval = max(interval, self.config.slow_interval_seconds)
            schedule.success(
                interval, started=getattr(runtime.connector, "_last_request", request_start_mono)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = classify_error(exc)
            schedule.failure(self.config, code)
            self.apply_exchange_cooldown(runtime, code, schedule.due)
            if runtime.schedules.get(market.id) is not schedule:
                return
            if runtime.cooldown_until > time.monotonic():
                schedule.due = max(schedule.due, runtime.cooldown_until)
            if code == "MALFORMED_RESPONSE":
                await self.store.malformed(
                    market.id,
                    getattr(runtime.connector, "last_public_excerpt", ""),
                    getattr(runtime.connector, "last_public_sha256", ""),
                )
                if runtime.schedules.get(market.id) is not schedule:
                    return
            previous = self.cache.get(market.id)
            if previous is not None and (
                confirmation or not self.native_book_fresh(runtime, market.id, self.utcnow())
            ):
                failed = previous.classified(
                    DataState.DEGRADED if previous.state == DataState.HEALTHY else previous.state,
                    reasons=tuple(dict.fromkeys((*previous.reasons, "last_request_failed"))),
                    verification={
                        **previous.verification,
                        "last_error_code": code,
                        "last_failure_at": self.utcnow().isoformat(),
                    },
                )
                self.cache.books[market.id] = failed
                await self.store.save_book(failed, force=True)
                if runtime.schedules.get(market.id) is not schedule:
                    return
            runtime.health["failed_requests"] += 1
            runtime.health["consecutive_failures"] += 1
            runtime.health.update(
                error_code=code,
                state="DEGRADED" if runtime.health["last_success_at"] else "UNAVAILABLE",
                updated_at=self.utcnow(),
                next_retry_at=self.utcnow()
                + timedelta(seconds=max(0, schedule.due - time.monotonic())),
            )
            await self.store.event(
                runtime.exchange_id,
                "exchange.request_failed",
                "Public order book request failed",
                "warning",
                {"error_code": code},
            )
            log.warning("public_request_failed", exchange=runtime.slug, error_code=code)
        finally:
            if confirmation and runtime.schedules.get(market.id) is schedule:
                runtime.confirmation.discard(market.id)
                self.confirmation_enqueued.pop(market.id, None)

    async def run_exchange(self, slug: str):
        runtime = self.runtimes[slug]
        try:
            runtime.exchange_id = await self.store.register_exchange(
                slug, runtime.connector.adapter.name, capabilities(runtime.connector.adapter)
            )
            while not self.stop.is_set():
                allowed = (self.terminal_market_preferences or {}).get("enabled_exchanges", [])
                if allowed and slug not in allowed:
                    try:
                        await asyncio.wait_for(self.stop.wait(), 2)
                    except TimeoutError:
                        pass
                    continue
                now = time.monotonic()
                if now < runtime.cooldown_until:
                    try:
                        await asyncio.wait_for(self.stop.wait(), 1)
                    except TimeoutError:
                        pass
                    continue
                if now >= runtime.discovery.due:
                    try:
                        await self.discover(runtime)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        code = classify_error(exc)
                        runtime.discovery.failure(self.config, code)
                        self.apply_exchange_cooldown(runtime, code, runtime.discovery.due)
                        runtime.discovery.due = max(runtime.discovery.due, runtime.cooldown_until)
                        runtime.health.update(
                            error_code=code,
                            updated_at=self.utcnow(),
                            state="DEGRADED" if runtime.markets else "UNAVAILABLE",
                            next_retry_at=self.utcnow()
                            + timedelta(seconds=max(0, runtime.discovery.due - time.monotonic())),
                        )
                        runtime.health["failed_requests"] += 1
                        await self.store.event(
                            runtime.exchange_id,
                            "exchange.discovery_failed",
                            "Public spot discovery failed",
                            "warning",
                            {"error_code": code},
                        )
                        log.warning("public_discovery_failed", exchange=slug, error_code=code)
                due = [
                    m for m in runtime.markets if runtime.schedules[m.id].due <= time.monotonic()
                ]
                if due:
                    market = min(due, key=lambda m: runtime.schedules[m.id].due)
                    fresh_ws = (
                        self.native_book_fresh(runtime, market.id, self.utcnow())
                        if runtime.slug in NATIVE_PROTOCOLS
                        else (
                            time.monotonic() - runtime.ws_last.get(market.id, -10000)
                            < self.config.maximum_age_seconds / 2
                        )
                    )
                    if fresh_ws and market.id not in runtime.confirmation:
                        runtime.transport_counts["rest_suppressed"] += 1
                        runtime.schedules[market.id].success(self.config.fast_interval_seconds)
                    else:
                        await self.fetch(runtime, market)
                if due:
                    await asyncio.sleep(0)  # Connector pacing owns the request rate.
                else:
                    delay = min(
                        (s.due - time.monotonic() for s in runtime.schedules.values()), default=1
                    )
                    try:
                        await asyncio.wait_for(self.stop.wait(), min(1, max(0.01, delay)))
                    except TimeoutError:
                        pass
        finally:
            if runtime.ws_task:
                runtime.ws_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runtime.ws_task
            await runtime.connector.close()

    async def housekeeping(self, lease):
        previous_states = {}
        prune_due = 0.0
        allocation_due = 0.0
        memory_due = time.monotonic() + 60
        while not self.stop.is_set():
            await lease.execute(text("SELECT 1"))  # Losing singleton ownership is fatal.
            await lease.commit()  # Keep the session lock without an idle transaction.
            self.clock.check(wall=self.utcnow())
            if time.monotonic() >= allocation_due and self.scanner:
                await self.reload_terminal_preferences()
                async with self.allocation_lock:
                    selected = self.monitoring_allocation()
                    for slug, chosen in selected.items():
                        rt = self.runtimes[slug]
                        if rt.exchange_id and {m.native_symbol for m in chosen} != {
                            m.native_symbol for m in rt.markets
                        }:
                            identified = await self.store.set_selection(rt.exchange_id, chosen)
                            await self.apply_selection(rt, identified)
                    for slug in NATIVE_PROTOCOLS.keys() & self.runtimes.keys():
                        await self.configure_native(self.runtimes[slug])
                    self.allocation_tick += 1
                allocation_due = time.monotonic() + 60
            if not self.clock.healthy:
                await self.store.event(
                    None, "collector.clock_jump", "Collector wall-clock drift detected", "warning"
                )
            while not self.confirmation_queue.empty():
                slug, market_id = self.confirmation_queue.get_nowait()
                runtime = self.runtimes[slug]
                if market_id in runtime.schedules and market_id in runtime.confirmation:
                    # Respect existing timeout/rate-limit backoff even for manual rechecks.
                    schedule = runtime.schedules[market_id]
                    if schedule.failures == 0:
                        schedule.due = 0
                self.confirmation_queue.task_done()
            counts = {state.value: 0 for state in DataState}
            for book in self.cache.books.values():
                current = self.cache.get(book.market_id)
                counts[current.state.value] += 1
            now = self.utcnow()
            for runtime in self.runtimes.values():
                if runtime.exchange_id is None:
                    continue
                health = runtime.health.copy()
                if health["last_success_at"] is None:
                    state = "UNAVAILABLE" if health["error_code"] else "DEGRADED"
                elif (
                    now - health["last_success_at"]
                ).total_seconds() > self.config.maximum_age_seconds:
                    state = "STALE"
                else:
                    states = [
                        self.cache.get(m.id).state for m in runtime.markets if self.cache.get(m.id)
                    ]
                    state = (
                        "HEALTHY"
                        if states
                        and all(s == DataState.HEALTHY for s in states)
                        and not health["error_code"]
                        else "DEGRADED"
                    )
                health.update(state=state, updated_at=now)
                runtime.health.update(state=state, updated_at=now)
                await self.store.health(runtime.exchange_id, health)
                if previous_states.get(runtime.slug) != state:
                    await self.store.event(
                        runtime.exchange_id,
                        "exchange.connectivity_changed",
                        "Exchange public-data connectivity changed",
                        "info" if state == "HEALTHY" else "warning",
                        {"state": state, "error_code": health["error_code"]},
                    )
                    previous_states[runtime.slug] = state
            clock = self.utc_clock.snapshot() if self.utc_clock else {"trusted": False}
            engine = self.scanner.diagnostics() if self.scanner else {}
            await self.sample_reliability(self.utcnow(), clock, engine)
            await self.store.heartbeat(
                {
                    "monitored_books": len(self.cache.books),
                    "quality_counts": counts,
                    "configured_exchanges": len(self.runtimes),
                    "pipeline_timings": self.timings.snapshot(),
                    "rejected_updates": dict(self.cache.rejected_updates),
                    "feed_timing_summary": {
                        slug: rt.timings.snapshot() for slug, rt in self.runtimes.items()
                    },
                    "clock_healthy": self.utc_clock.trusted if self.utc_clock else False,
                    "clock": clock,
                    "engine": engine,
                    "paper": self.paper.diagnostics() if self.paper else {},
                    "reliability": self.reliability.snapshot,
                }
            )
            self.last_heartbeat = time.monotonic()
            if time.monotonic() >= memory_due:
                self.memory_maintenance = await asyncio.to_thread(reclaim_unused_pages)
                memory_due = time.monotonic() + self.config.memory_maintenance_interval_seconds
            if time.monotonic() >= prune_due:
                await self.store.prune()
                prune_due = time.monotonic() + 3600
            try:
                await asyncio.wait_for(self.stop.wait(), 5)
            except TimeoutError:
                pass

    async def serve(self):
        import hmac

        token = self.settings.collector_internal_token.get_secret_value()

        async def health(request):
            ready = (
                (self.last_heartbeat > 0 or not self.config.enabled)
                and (not self.config.enabled or time.monotonic() - self.last_heartbeat < 30)
                and not self.stop.is_set()
            )
            return web.json_response(
                {"status": "ready" if ready else "not_ready", "mode": "paper"},
                status=200 if ready else 503,
            )

        @web.middleware
        async def authenticate(request, handler):
            if request.path != "/health" and (
                not token
                or not hmac.compare_digest(
                    request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()
                )
            ):
                return web.json_response({"detail": "Internal authentication required"}, status=401)
            return await handler(request)

        async def book(request):
            try:
                market_id = UUID(request.match_info["market_id"])
            except ValueError:
                return web.json_response({"detail": "Invalid market ID"}, status=422)
            value = self.cache.get(market_id)
            if value is None:
                return web.json_response({"detail": "No collected book available"}, status=404)
            return web.json_response(value.model_dump(mode="json"))

        async def confirm(request):
            try:
                market_id = UUID(request.match_info["market_id"])
            except ValueError:
                return web.json_response({"detail": "Invalid market ID"}, status=422)
            result = self.request_confirmation(market_id)
            return web.json_response(
                {"status": result},
                status=202 if result == "QUEUED" else 409 if result == "NOT_MONITORED" else 429,
            )

        async def diagnostics(request):
            return web.json_response(
                self.scanner.diagnostics() if self.scanner else {"healthy": False}
            )

        async def current_routes(request):
            values = list(self.scanner.current.values()) if self.scanner else []
            if request.query.get("route_id"):
                values = [x for x in values if x["id"] == request.query["route_id"]]
            elif request.query.get("route_ids"):
                ids = set(request.query["route_ids"].split(","))
                if len(ids) > 50:
                    return web.json_response({"detail": "At most50route IDs"}, status=422)
                values = [x for x in values if x["id"] in ids]
            if request.query.get("summary") == "1":
                summarized = []
                for value in values[:500]:
                    evidence = value.get("evidence", {})
                    inputs = evidence.get("calculation_inputs", {})
                    timing = evidence.get("timing", {})
                    fee_assumptions = evidence.get("fee_assumptions", {})
                    small_inputs = {"evaluated_at": inputs.get("evaluated_at")}
                    small_fees = {}
                    for side in ("buy", "sell"):
                        leg = inputs.get(side, {})
                        fx = leg.get("fx") or {}
                        small_inputs[side] = {
                            "quote_currency": leg.get("quote_currency"),
                            "fx": {"fresh_until": fx.get("fresh_until")},
                            "fee": {"verified": (leg.get("fee") or {}).get("verified")},
                        }
                        fee = fee_assumptions.get(side, {})
                        small_fees[side] = {
                            name: fee.get(name)
                            for name in (
                                "fee_evidence_status",
                                "account_tier_verified",
                                "fee_currency_assumed",
                            )
                        }
                    summarized.append(
                        {
                            **{
                                k: v
                                for k, v in value.items()
                                if k
                                not in (
                                    "evidence",
                                    "budget_results",
                                    "transfer_scenario",
                                    "maximum_executable",
                                )
                            },
                            "maximum_executable": {
                                "acquisition_cost_cad": (value.get("maximum_executable") or {}).get(
                                    "acquisition_cost_cad"
                                )
                            },
                            "evidence": {
                                "buy_quality": evidence.get("buy_quality"),
                                "sell_quality": evidence.get("sell_quality"),
                                "calculation_inputs": small_inputs,
                                "fee_assumptions": small_fees,
                                "timing": {
                                    name: timing.get(name)
                                    for name in (
                                        "clock_trusted",
                                        "cross_book_receive_skew_seconds",
                                        "cross_book_source_skew_seconds",
                                        "cross_book_uncertainty_seconds",
                                    )
                                },
                            },
                        }
                    )
                values = summarized
            return web.json_response(
                {
                    "items": values[:500],
                    "diagnostics": self.scanner.diagnostics()
                    if self.scanner
                    else {"healthy": False},
                }
            )

        async def clock_status(request):
            return web.json_response(
                self.utc_clock.snapshot() if self.utc_clock else {"trusted": False}
            )

        async def route_recheck(request):
            from arbiq.db.models import ArbitrageRoute

            try:
                route_id = UUID(request.match_info["route_id"])
            except ValueError:
                return web.json_response({"detail": "Invalid ID"}, status=422)
            async with self.store.sessions() as db:
                route = await db.get(ArbitrageRoute, route_id)
                if route is None:
                    return web.json_response({"detail": "Route unavailable"}, status=404)
                outcomes = [
                    self.request_confirmation(x)
                    for x in (route.buy_market_id, route.sell_market_id)
                ]
            if "QUEUED" in outcomes:
                await self.scanner.store.transition(
                    route_id, "RECHECKING", "FRESH_BOOKS_REQUESTED", self.utcnow()
                )
                for body in self.scanner.current.values():
                    if body["id"] == str(route_id):
                        body["status"] = "RECHECKING"
                self.scanner.metrics["rechecked"] += 1
                return web.json_response(
                    {"status": "RECHECKING", "outcomes": outcomes, "actionable": False}, status=202
                )
            return web.json_response(
                {"status": "RECHECK_UNAVAILABLE", "outcomes": outcomes}, status=429
            )

        async def paper_status(request):
            return web.json_response(self.paper.diagnostics() if self.paper else {"healthy": False})

        async def terminal_runtime(request):
            now = self.utcnow()
            clock = self.utc_clock.snapshot() if self.utc_clock else {"trusted": False}
            books = [self.cache.get(x, now=now) for x in list(self.cache.books)]
            books = [x for x in books if x is not None]
            ages = [age_seconds(now, x.received_at) for x in books] if clock.get("trusted") else []
            ages = [age for age in ages if age is not None]
            source_ages = {}
            if clock.get("trusted"):
                for book in books:
                    source_ages.setdefault(book.source, []).append(
                        age_seconds(now, book.received_at)
                    )
            stamp = elapsed_seconds()
            synchronized_ids = set()
            for runtime in self.runtimes.values():
                last_message = runtime.ws_last_message_elapsed
                if (
                    last_message is not None
                    and 0 <= stamp - last_message <= self.config.websocket_idle_timeout_seconds
                ):
                    synchronized_ids.update(runtime.native_synchronized)
                    if runtime.slug == "kraken" and runtime.ws_synchronized:
                        synchronized_ids.update(runtime.ws_last)
            latencies = list(self.api_latencies)
            healthy = self.last_heartbeat > 0 and time.monotonic() - self.last_heartbeat < 30
            return web.json_response(
                {
                    "as_of": now.isoformat(),
                    "collector": {
                        "healthy": healthy,
                        "reliability": self.reliability.snapshot if healthy else None,
                        "cache_books": len(books),
                        "cache_capacity": self.cache.limit,
                        "quality_counts": dict(Counter(x.state.value for x in books)),
                        "book_sources": dict(Counter(x.source for x in books)),
                        "asset_coverage": asset_coverage(
                            {slug: runtime.markets for slug, runtime in self.runtimes.items()},
                            books,
                            synchronized_ids,
                            self.scanner.current.values() if self.scanner else None,
                            config=self.config,
                            now=now,
                            clock=clock,
                        ),
                        "transport_sources": {
                            source: distribution(receipt_ages)
                            for source, receipt_ages in source_ages.items()
                        },
                        "freshness_seconds": {
                            "median": median(ages) if ages else None,
                            "maximum": max(ages) if ages else None,
                            **(distribution(ages) if clock.get("trusted") else {}),
                        },
                        "api_latency_ms": {
                            "sample_count": len(latencies),
                            "mean": sum(latencies) / len(latencies) if latencies else None,
                            "peak": max(latencies) if latencies else None,
                            "window_limit": 600,
                        },
                        "recheck_queue": {
                            "pending": len(self.confirmation_enqueued),
                            "limit": self.confirmation_queue.maxsize,
                            "oldest_seconds": max(
                                (
                                    elapsed_seconds() - t
                                    for t in self.confirmation_enqueued.values()
                                ),
                                default=0,
                            ),
                        },
                        "pipeline_timings": self.timings.snapshot(),
                        "rejected_updates": dict(self.cache.rejected_updates),
                        "scheduling_backlog": sum(
                            s.due <= time.monotonic()
                            for rt in self.runtimes.values()
                            for s in rt.schedules.values()
                        ),
                        "applied_settings_version": self.applied_terminal_version,
                        "applied_market_preferences": self.terminal_market_preferences,
                        "native_metadata_cache": {
                            slug: rt.connector.metadata_metrics
                            for slug, rt in self.runtimes.items()
                            if hasattr(rt.connector, "metadata_metrics")
                        },
                        "memory_maintenance": self.memory_maintenance,
                    },
                    "scanner": self.scanner.diagnostics() if self.scanner else {"healthy": False},
                    "paper": self.paper.diagnostics() if self.paper else {"healthy": False},
                    "telemetry": self.telemetry.sample(),
                }
            )

        async def funding_marks(request):
            if not self.paper or not self.utc_clock or not self.utc_clock.trusted:
                return web.json_response({"detail": "Trusted market UTC unavailable"}, status=503)
            try:
                body = await request.json()
                items = body["items"]
                if not isinstance(items, list) or not 1 <= len(items) <= 100:
                    raise ValueError("Bounded items required")
                parsed = []
                for item in items:
                    quantity = exact_decimal(item["quantity"])
                    if quantity <= 0:
                        raise ValueError("Positive quantity required")
                    parsed.append((UUID(item["exchange_id"]), UUID(item["asset_id"]), quantity))
            except (ValueError, TypeError, KeyError):
                return web.json_response({"detail": "Invalid bounded mark request"}, status=422)
            now = self.utcnow()
            result = []
            for exchange, asset, quantity in parsed:
                mark = self.paper.market.mark(exchange, asset, quantity, now, research=True)
                result.append(
                    {
                        "exchange_id": str(exchange),
                        "asset_id": str(asset),
                        "quantity": str(quantity),
                        **safe_json(asdict(mark)),
                    }
                )
            return web.json_response(
                {"items": result, "clock": self.utc_clock.snapshot(), "as_of": now.isoformat()}
            )

        async def portfolio(request):
            from arbiq.db.models import PaperSession
            from arbiq.paper_trading.models import PaperControl
            from arbiq.portfolio.models import PortfolioPosition
            from arbiq.portfolio.valuation import snapshot

            try:
                session_id = UUID(request.match_info["session_id"])
            except ValueError:
                return web.json_response({"detail": "Invalid session"}, status=422)
            async with self.store.sessions() as db:
                control = await db.get(PaperControl, session_id)
                session = await db.get(PaperSession, session_id)
                if not control or not session:
                    return web.json_response({"detail": "Session not found"}, status=404)
                now = self.utcnow()
                positions = list(
                    await db.scalars(
                        select(PortfolioPosition).where(PortfolioPosition.session_id == session_id)
                    )
                )
                prices = self.paper.market.prices(
                    positions, now, control.classification == "RESEARCH"
                )
                benchmark = {}
                for h in session.benchmark_snapshot.get("holdings", []):
                    key = (UUID(h["exchange_id"]), UUID(h["asset_id"]))
                    benchmark[key] = self.paper.market.mark(
                        *key, Decimal(h["quantity"]), now, control.classification == "RESEARCH"
                    )
                value = await snapshot(
                    db,
                    session_id,
                    prices,
                    now,
                    research_only=control.classification == "RESEARCH",
                    persist=False,
                    benchmark_prices=benchmark,
                )
                return web.json_response(value)

        app = web.Application(middlewares=[authenticate], client_max_size=32768)
        app.router.add_get("/health", health)
        app.router.add_get("/clock", clock_status)

        async def feed_status(request):
            return web.json_response(self.market_status())

        app.router.add_get("/feeds/status", feed_status)
        app.router.add_get("/paper/status", paper_status)
        app.router.add_post("/paper/funding/marks", funding_marks)
        app.router.add_get("/terminal/runtime", terminal_runtime)
        app.router.add_get("/paper/portfolios/{session_id}", portfolio)
        app.router.add_get("/engine/current", current_routes)
        app.router.add_get("/engine/diagnostics", diagnostics)
        app.router.add_post("/engine/routes/{route_id}/recheck", route_recheck)
        app.router.add_get("/books/{market_id}", book)
        app.router.add_post("/books/{market_id}/confirm", confirm)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 8001).start()
        try:
            await self.stop.wait()
        finally:
            await runner.cleanup()

    async def run(self, lease):
        self.utc_clock = TrustedClock()
        self.cache.now = self.utcnow
        self.scanner = ArbitrageEngine(self)
        self.paper = PaperCoordinator(self)
        supported, unsupported = available_candidates(
            self.config.exchange_ids, self.config.exclude_exchanges
        )
        await self.store.disable_unconfigured(set(supported + unsupported))
        for slug in supported:
            self.runtimes[slug] = ExchangeRuntime(slug, PublicConnector(slug, self.config))
            self.runtimes[slug].connector.utcnow = self.utcnow
            self.runtimes[slug].connector._request_gate = self.gate
            self.runtimes[slug].connector._metadata_gate = self.metadata_gate
        for slug in unsupported:
            exchange_id = await self.store.register_exchange(
                slug,
                slug,
                {
                    "public_market_data_supported": False,
                    "canadian_eligibility": eligibility(slug),
                    "error": "UNSUPPORTED_ADAPTER",
                },
            )
            await self.store.event(
                exchange_id,
                "exchange.unsupported",
                "Configured adapter absent from installed CCXT",
                "warning",
            )
        self.tasks = [asyncio.create_task(self.run_exchange(slug), name=slug) for slug in supported]
        self.tasks.extend(
            [
                asyncio.create_task(self.housekeeping(lease), name="housekeeping"),
                asyncio.create_task(self.synchronize_clock(), name="trusted_utc"),
                asyncio.create_task(self.scanner.run(), name="arbitrage_scanner"),
                asyncio.create_task(self.paper.run(), name="paper_simulator"),
                asyncio.create_task(self.serve(), name="internal_api"),
            ]
        )

        async def fail_closed(task):
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "collector_task_failed", task=task.get_name(), error_type=type(exc).__name__
                )
                self.stop.set()
                raise

        watchers = [asyncio.create_task(fail_closed(task)) for task in self.tasks]
        try:
            await self.stop.wait()
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            results = await asyncio.gather(*watchers, return_exceptions=True)
            if any(isinstance(value, Exception) for value in results):
                raise RuntimeError("Collector task failed; process restart required")


async def main():
    settings = get_settings()
    configure_logging(settings.log_level)
    if len(settings.collector_internal_token.get_secret_value()) < 32:
        raise RuntimeError("Generate COLLECTOR_INTERNAL_TOKEN before starting the collector")
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        # One singleton lease plus one warm transaction; four total connections
        # remain available during bursts, with no reduction in query concurrency.
        pool_size=2,
        max_overflow=2,
        pool_use_lifo=True,
        hide_parameters=True,
        connect_args={"server_settings": {"timezone": "UTC"}},
    )
    store = CollectorStore(async_sessionmaker(engine, expire_on_commit=False), settings.collector)
    worker = Collector(settings, store)
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, worker.stop.set)
    try:
        if not settings.collector.enabled:
            log.info("collector_disabled")
            await worker.serve()
            return
        async with engine.connect() as lease:
            if (
                await lease.scalar(text("SELECT version_num FROM alembic_version"))
                != EXPECTED_SCHEMA_REVISION
            ):
                raise RuntimeError("Migrate database before starting collector")
            if not await lease.scalar(text("SELECT pg_try_advisory_lock(731093)")):
                raise RuntimeError("A collector already owns this database")
            await lease.commit()
            await store.start(f"{socket.gethostname()}:{uuid4()}")
            try:
                await worker.run(lease)
            finally:
                await store.stop()
                await lease.execute(text("SELECT pg_advisory_unlock(731093)"))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
