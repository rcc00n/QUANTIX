"""Incremental, bounded scanner embedded in the singleton public collector.

Only normalized, monitored identities enter the index; the discovered catalog is
never quadratically compared. This module never submits orders or creates fills.
"""

import asyncio
import hashlib
import time
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from arbiq.core.clock import elapsed_seconds
from arbiq.market_data.quality import DataState, safe_json
from arbiq.market_data.timing import Timings, age_seconds
from arbiq.opportunities.contracts import (
    CALCULATION_VERSION,
    ConversionRoute,
    Costs,
    Fee,
    FXConversion,
    MarketInput,
    MarketRules,
    identity_cad,
)
from arbiq.opportunities.coverage import metrics as coverage_metrics
from arbiq.opportunities.coverage import venue_group
from arbiq.opportunities.evidence import EvidencePool, shared_json
from arbiq.opportunities.math import conversion_leg_from_book, optimize_route
from arbiq.opportunities.projection_cache import ProjectionCache
from arbiq.opportunities.storage import OpportunityStore

ANOMALY_REASONS = {
    "extreme_reference_discrepancy",
    "extreme_temporal_discrepancy",
    "unconfirmed_anomaly",
}


def book_reasons(book, now, config):
    reasons = []
    age = (now - book.received_at).total_seconds()
    if age < 0 or age > config.maximum_book_age_seconds:
        reasons.append("QUOTE_INGESTION_NOT_CURRENT")
    if book.exchange_timestamp is None:
        reasons.append("SOURCE_TIMESTAMP_UNAVAILABLE")
    elif (
        not 0 <= (now - book.exchange_timestamp).total_seconds() <= config.maximum_book_age_seconds
    ):
        reasons.append("SOURCE_TIMESTAMP_NOT_CURRENT")
    reasons.extend(book.reasons)
    if book.state in {DataState.STALE, DataState.UNAVAILABLE}:
        reasons.append("BOOK_" + book.state.value)
    if book.state == DataState.SUSPICIOUS:
        reasons.append("ANOMALY_REQUIRES_FRESH_INDEPENDENT_RECHECK")
    if book.verification.get("last_error_code"):
        reasons.append("EXCHANGE_LAST_REQUEST_FAILED")
    return list(dict.fromkeys(reasons))


class ArbitrageEngine:
    def __init__(self, collector):
        self.collector = collector
        self.config = collector.settings.arbitrage
        self.store = OpportunityStore(collector.store.sessions, collector.store.run_id)
        self.pending = set()
        self.wake = asyncio.Event()
        self.index = defaultdict(set)
        self.market_map = {}
        self.current = {}
        self.last_write = {}
        self.writes = deque()
        self.last_seen = {}
        self.pending_transitions = {}
        self.metrics = Counter()
        self.invalidations = Counter()
        self.latencies = deque(maxlen=100)
        self.timings = Timings()
        self.heartbeat = 0.0
        self.fx_evidence = {}
        self.jobs = OrderedDict()
        self.seen_candidates = OrderedDict()
        self.last_evaluated = OrderedDict()
        self.selection = ()
        self.negative_writes = deque()
        self.base_rotation = 0
        self.projections = ProjectionCache()
        self.evidence_pool = EvidencePool(
            getattr(collector.cache, "limit", self.config.max_candidates)
        )

    def changed(self, book):
        if len(self.pending) < self.config.max_candidates:
            self.pending.add(book.base_key)
            self.wake.set()
        else:
            self.metrics["coalesced_updates"] += 1

    def rebuild(self):
        self.index.clear()
        self.market_map.clear()
        for rt in self.collector.runtimes.values():
            for market in rt.markets:
                self.market_map[market.id] = (rt, market)
                if market.base.verified and market.quote.verified and market.active is not False:
                    self.index[market.base.key].add(market.id)

        self.selection = tuple(rt.markets for rt in self.collector.runtimes.values())
        self.evidence_pool.retain(self.market_map)
        self.projections.retain(self.market_map)

    def fee_rate(self, market):
        advertised = market.metadata.get("advertised_taker_rate")
        try:
            value = Decimal(advertised) if advertised is not None else Decimal(0)
            if not value.is_finite() or not 0 <= value < 1:
                value = Decimal(0)
        except (ValueError, TypeError, ArithmeticError):
            value = Decimal(0)
        return max(self.config.conservative_taker_rate, value)

    def fx_routes(self, now):
        """Bounded two-hop executable currency graph; USD/USDC/USDT remain distinct.

        Conversion books are independent of traded crypto legs. Quotes are liquidation
        bids and acquisition asks, including taker estimates and observed depth.
        """
        allowed = {"CAD", "USD", "USDC", "USDT", "EUR"}
        graph = defaultdict(list)
        self.fx_evidence = {}
        for market_id, (rt, market) in self.market_map.items():
            if (
                not market.base.verified
                or not market.quote.verified
                or market.active is not True
                or rt.health["error_code"]
                or market.base.symbol not in allowed
                or market.quote.symbol not in allowed
            ):
                continue
            book = self.collector.cache.get(market_id, now=now)
            if (
                book is None
                or book.state != DataState.HEALTHY
                or book_reasons(book, now, self.config)
            ):
                continue
            for reverse in (False, True):
                leg = conversion_leg_from_book(
                    market.base.symbol,
                    market.quote.symbol,
                    bids=book.normalized.bids,
                    asks=book.normalized.asks,
                    reverse=reverse,
                    fee_rate=self.fee_rate(market),
                    evidence=str(market_id) + ":" + book.content_sha256,
                )
                graph[leg.source_currency].append((leg, book))
            self.fx_evidence[str(market_id)] = self.evidence_pool.book(book)

        def find(source, target):
            queue = deque([(source, (), (), {source})])
            while queue:
                currency, path, books, visited = queue.popleft()
                if currency == target:
                    return path, books
                if len(path) >= 2:
                    continue
                for leg, book in sorted(graph[currency], key=lambda x: x[0].evidence):
                    if leg.target_currency not in visited:
                        queue.append(
                            (
                                leg.target_currency,
                                (*path, leg),
                                (*books, book),
                                visited | {leg.target_currency},
                            )
                        )
            return None

        result = {"CAD": identity_cad()}
        for currency in allowed - {"CAD"}:
            to, back = find(currency, "CAD"), find("CAD", currency)
            if not to or not back:
                continue
            books = (*to[1], *back[1])
            fresh = min(
                book.received_at + timedelta(seconds=self.config.maximum_book_age_seconds)
                for book in books
            )
            result[currency] = FXConversion(
                currency,
                ConversionRoute(to[0]),
                ConversionRoute(back[0]),
                False,
                fresh,
                "Executable public conversion books; "
                "account fee and funding assumptions unverified",
                observed_at=max(book.exchange_timestamp or book.received_at for book in books),
                available_at=max(book.received_at for book in books),
            )
        return result

