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
from arbiq.finance.contracts import exact_decimal
from arbiq.finance.fees import FeeAssessment, assess_fee
from arbiq.market_data.quality import DataState, safe_json
from arbiq.market_data.timing import Timings, age_seconds
from arbiq.market_data.trading_rules import positive_decimal
from arbiq.opportunities.comparison_queue import ComparisonQueue
from arbiq.opportunities.contracts import (
    CALCULATION_VERSION,
    Costs,
    Fee,
    MarketInput,
    MarketRules,
)
from arbiq.opportunities.conversions import (
    FX_ASSET_KEYS,
    ConversionSnapshot,
    FXBook,
    capture_uncertainty_ms,
    select_cad_paths,
    spot_conversion_leg,
    synchronized_conversion,
)
from arbiq.opportunities.coverage import metrics as coverage_metrics
from arbiq.opportunities.coverage import venue_group
from arbiq.opportunities.evidence import EvidencePool, shared_json
from arbiq.opportunities.math import optimize_route
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
        self.pending_markets = set()
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
        self.fx_markets = ()
        self.fx_cache_key = None
        self.fx_snapshot = None
        self.comparisons = ComparisonQueue()
        self.jobs = self.comparisons.ready
        self.seen_candidates = OrderedDict()
        self.last_evaluated = OrderedDict()
        self.selection = ()
        self.negative_writes = deque()
        self.fx_inputs = {}
        self.scheduler_context = None
        self.projections = ProjectionCache()
        self.evidence_pool = EvidencePool(
            getattr(collector.cache, "limit", self.config.max_candidates)
        )

    def changed(self, book):
        if book.base_key in self.pending or len(self.pending) < self.config.max_candidates:
            self.pending.add(book.base_key)
        else:
            self.metrics["coalesced_updates"] += 1
        # Delivery is bounded by the current monitored market index. The asset
        # telemetry cap does not drop a valid selected market's update.
        if book.market_id in self.market_map:
            if book.market_id in self.pending_markets:
                self.metrics["comparison_market_updates_coalesced"] += 1
            self.pending_markets.add(book.market_id)
            self.wake.set()

    def rebuild(self):
        self.index.clear()
        self.market_map.clear()
        for rt in self.collector.runtimes.values():
            for market in rt.markets:
                self.market_map[market.id] = (rt, market)
                if market.base.verified and market.quote.verified and market.active is not False:
                    self.index[market.base.key].add(market.id)

        self.selection = tuple(rt.markets for rt in self.collector.runtimes.values())
        self.fx_markets = tuple(
            (runtime, market, base, quote)
            for runtime, market in self.market_map.values()
            if (base := FX_ASSET_KEYS.get(market.base.key))
            and (quote := FX_ASSET_KEYS.get(market.quote.key))
            and base != quote
            and market.base.verified
            and market.quote.verified
            and market.active is True
            and (market.metadata.get("trading_rules") or {}).get("market_order_available")
            is not False
        )
        self.fx_cache_key = None
        self.metrics["comparison_work_rebuilt"] += self.comparisons.rebuild(
            self.market_map, self.index, elapsed_seconds()
        )
        self.pending_markets.intersection_update(self.market_map)
        self.evidence_pool.retain(self.market_map)
        self.projections.retain(self.market_map)

    def fee_assessment(
        self, market, *, now: datetime | None = None, adapter: str | None = None
    ) -> FeeAssessment:
        if adapter is None:
            entry = self.market_map.get(market.id)
            adapter = entry[0].slug if entry else ""
        metadata = market.metadata
        return assess_fee(
            adapter,
            market.base.symbol,
            market.quote.symbol,
            configured_taker_floor=self.config.conservative_taker_rate,
            advertised_taker=metadata.get("advertised_taker_rate"),
            now=now if now is not None else self.collector.utcnow(),
            fx_stablecoin=metadata.get("fx_stablecoin") is True,
        )

    def fee_rate(self, market, *, now: datetime | None = None) -> Decimal:
        return self.fee_assessment(market, now=now).applied_taker_rate

    def fx_routes(self, now):
        """Select bounded causal depth paths and pin the underlying books."""
        age = timedelta(seconds=self.config.maximum_book_age_seconds)
        skew = timedelta(seconds=self.config.maximum_book_skew_seconds)
        selected = []
        with_observation = [age, skew, self.config.default_budget_cad]
        for rt, market, base, quote in self.fx_markets:
            if rt.health["error_code"]:
                continue
            book = self.collector.cache.get(market.id, now=now)
            if (
                book is None
                or book.state != DataState.HEALTHY
                or book_reasons(book, now, self.config)
            ):
                continue
            clock = book.verification.get("clock") or {}
            uncertainty = capture_uncertainty_ms(clock)
            if (
                clock.get("trusted") is False
                or not book.exchange_timestamp <= book.received_at <= now
                or now - book.exchange_timestamp + timedelta(milliseconds=uncertainty) > age
            ):
                continue
            fee = self.fee_assessment(market, now=now, adapter=rt.slug)
            selected.append((base, quote, book, fee, uncertainty))
            with_observation.append(
                (
                    book.market_id,
                    book.content_sha256,
                    book.source,
                    book.exchange_timestamp,
                    book.received_at,
                    uncertainty,
                    fee,
                )
            )
        cache_key = tuple(with_observation)
        if cache_key == self.fx_cache_key:
            self.metrics["fx_graph_reuses"] += 1
            return self.fx_snapshot

        edges = []
        sources = {}
        for base, quote, book, fee, uncertainty in selected:
            reference = str(book.market_id) + ":" + book.content_sha256
            levels = {
                "bids": book.normalized.bids,
                "asks": book.normalized.asks,
                "fee": fee,
                "reference": reference,
            }
            edges.append(
                FXBook(
                    spot_conversion_leg(base, quote, **levels, side="sell"),
                    spot_conversion_leg(base, quote, **levels, side="buy"),
                    book.exchange_timestamp,
                    book.received_at,
                    uncertainty,
                )
            )
            sources[reference] = book, fee
        result = select_cad_paths(
            edges,
            now=now,
            age=age,
            skew=skew,
            probe_cad=self.config.default_budget_cad,
        )
        used = {leg.evidence for fx in result.values() for leg in fx.to_cad.legs}
        evidence = {
            key.split(":", 1)[0]: {
                **self.evidence_pool.book(sources[key][0]),
                "fees": {side: sources[key][1].as_evidence(side) for side in ("buy", "sell")},
            }
            for key in sorted(used)
        }
        self.fx_snapshot = ConversionSnapshot(result, evidence)
        self.fx_cache_key = cache_key
        self.metrics["fx_graph_rebuilds"] += 1
        return self.fx_snapshot

    def market_input(self, rt, market, book, fx, *, fee: FeeAssessment, side):
        metadata = market.metadata

        def optional_limit(name):
            raw = metadata.get(name)
            value = exact_decimal(raw) if raw is not None else None
            return value if value is not None and value > 0 else None

        native = metadata.get("trading_rules") or {}
        observed = native.get("observed_at")
        observed_at = datetime.fromisoformat(observed) if observed else None
        min_quantity = optional_limit("minimum_quantity")
        min_notional = positive_decimal(market.min_notional)
        legacy_verified = bool(
            market.amount_step and min_quantity and metadata.get("active_status") == "active"
        )
        limitations = (native["limitation"],) if native.get("limitation") else ()
        rules = MarketRules(
            market.amount_step,
            min_quantity,
            min_notional,
            optional_limit("maximum_quantity"),
            optional_limit("maximum_notional"),
            verified=legacy_verified
            and (
                not native
                or (
                    native.get("market_order_available") is True
                    and observed_at is not None
                    and market.price_tick is not None
                    and min_notional is not None
                    and not limitations
                )
            ),
            source=native.get("source", "CCXT spot metadata with retained precision and limits"),
            price_tick=market.price_tick,
            market_order_available=native.get("market_order_available"),
            cost_step=positive_decimal(native.get("cost_step")),
            version=native.get("version"),
            observed_at=observed_at,
            native_status=native.get("native_status"),
            restrictions=tuple(native.get("status_flags", ())),
            limitations=limitations,
            external_notional_limits=tuple(
                (key, Decimal(native[key]))
                for key in ("maxMktSz_USDT", "maxMktAmt_USD")
                if key in native
            ),
        )
        return MarketInput(
            str(market.id),
            venue_group(rt.slug),
            market.base.key,
            market.quote.symbol,
            book.normalized.asks,
            book.normalized.bids,
            Fee(
                fee.applied_taker_rate,
                currency=fee.currency(side),
                verified=False,
                source="Conservative taker estimate with retained public fee evidence",
                version=fee.version,
            ),
            rules,
            fx.get(market.quote.symbol),
        )

    async def evaluate_pair(self, buy_id, sell_id, fx, now, *, books=None):
        evaluation_started = elapsed_seconds()
        br, bm = self.market_map[buy_id]
        sr, sm = self.market_map[sell_id]
        if venue_group(br.slug) == venue_group(sr.slug):
            return
        buy, sell = (
            books
            if books is not None
            else (
                self.collector.cache.get(buy_id, now=now),
                self.collector.cache.get(sell_id, now=now),
            )
        )
        if not buy or not sell or not buy.normalized.asks or not sell.normalized.bids:
            return
        key = hashlib.sha256(f"{buy_id}:{sell_id}".encode()).hexdigest()
        if "stream_unsynchronized" in buy.reasons or "stream_unsynchronized" in sell.reasons:
            await self.reject_unsynchronized(key, now)
            return
        stream_epochs = [
            (rt, rt.native_integrity_epoch)
            for rt, book in ((br, buy), (sr, sell))
            if book.source == f"{rt.slug}_ws" and hasattr(rt, "native_integrity_epoch")
        ]
        preferences = getattr(self.collector, "terminal_market_preferences", None) or {}
        if not preferences.get("research_anomaly_scanning", True) and (
            buy.state == DataState.SUSPICIOUS or sell.state == DataState.SUSPICIOUS
        ):
            if key in self.current:
                await self.invalidate(key, "INVALIDATED", "ANOMALY_RESEARCH_DISABLED", now)
            self.metrics["anomaly_candidates_disabled_by_policy"] += 1
            return  # Underlying quality events/books remain retained independently.
        if key in self.pending_transitions:
            await self.flush_transitions()
            if key in self.pending_transitions:
                return
        skew_limit = timedelta(seconds=self.config.maximum_book_skew_seconds)
        buy_fx = fx.get(FX_ASSET_KEYS.get(bm.quote.key))
        sell_fx = fx.get(FX_ASSET_KEYS.get(sm.quote.key))
        rejected_fx = False
        if not synchronized_conversion(
            buy_fx,
            buy.exchange_timestamp,
            buy.received_at,
            capture_uncertainty_ms(buy.verification.get("clock") or {}),
            skew_limit,
        ):
            buy_fx, rejected_fx = None, True
        if not synchronized_conversion(
            sell_fx,
            sell.exchange_timestamp,
            sell.received_at,
            capture_uncertainty_ms(sell.verification.get("clock") or {}),
            skew_limit,
        ):
            sell_fx, rejected_fx = None, True
        try:
            # Screen with executable best bid/ask, never ticker/midprice. Cross-quote
            # screening is only possible with explicit directional CAD evidence.
            if bm.quote.key == sm.quote.key:
                raw_positive = sell.normalized.bids[0].price > buy.normalized.asks[0].price
            elif buy_fx and sell_fx:
                from arbiq.opportunities.math import convert, required_input

                probe = min(
                    buy.normalized.asks[0].quantity,
                    sell.normalized.bids[0].quantity,
                    Decimal("0.000001"),
                )
                raw_positive = convert(
                    sell_fx.to_cad, sell.normalized.bids[0].price * probe
                ) > required_input(buy_fx.from_cad, buy.normalized.asks[0].price * probe)
            else:
                if key in self.current and self.current[key]["status"] not in {
                    "INVALIDATED",
                    "EXPIRED",
                }:
                    reason = "FX_CAPTURE_SKEW_EXCEEDED" if rejected_fx else "FX_PATH_UNAVAILABLE"
                    await self.invalidate(key, "INVALIDATED", reason, now)
                return
            if not raw_positive:
                if key in self.current and self.current[key]["status"] not in {
                    "INVALIDATED",
                    "EXPIRED",
                }:
                    await self.invalidate(key, "INVALIDATED", "OBSERVED_SPREAD_DISAPPEARED", now)
                return
            buy_fee = self.fee_assessment(bm, now=now, adapter=br.slug)
            sell_fee = self.fee_assessment(sm, now=now, adapter=sr.slug)
            bi = self.market_input(br, bm, buy, {bm.quote.symbol: buy_fx}, fee=buy_fee, side="buy")
            si = self.market_input(
                sr, sm, sell, {sm.quote.symbol: sell_fx}, fee=sell_fee, side="sell"
            )
            costs = Costs(
                slippage_bps=self.config.slippage_bps, rebalancing_bps=self.config.rebalancing_bps
            )
            config = self.config
            projection_input = self.projections.signature(
                bi, si, buy.normalized, sell.normalized, config, costs, now
            )
            result = self.projections.lookup(key, projection_input)
            if result is None:
                # Decimal optimization is CPU work; keep network/clock checks and
                # shutdown responsive even when the economic inputs have changed.
                result = await asyncio.to_thread(
                    optimize_route,
                    bi,
                    si,
                    budget_cad=config.default_budget_cad,
                    now=now,
                    costs=costs,
                    budgets=(),
                    max_search_evaluations=config.max_optimizer_evaluations,
                )
                self.projections.remember(key, projection_input, result, config.max_candidates)
                self.metrics["optimizer_fresh_calculations"] += 1
            else:
                self.metrics["optimizer_exact_reuses"] += 1
        except (ValueError, ArithmeticError):
            if key in self.current and self.current[key]["status"] not in {
                "INVALIDATED",
                "EXPIRED",
            }:
                await self.invalidate(
                    key, "INVALIDATED", "INVALID_MARKET_METADATA_OR_INSUFFICIENT_DEPTH", now
                )
            self.invalidations["INVALID_MARKET_METADATA_OR_INSUFFICIENT_DEPTH"] += 1
            return
        self.last_seen[key] = time.monotonic()
        self.metrics["evaluations"] += 1
        best = result.best
        cad_budget_unavailable = (
            best is None
            and result.maximum is not None
            and result.maximum.acquisition_cost_cad is None
        )
        if cad_budget_unavailable:
            best = result.maximum
        completed_at = self.collector.utcnow()
        # A socket can lose synchronization while Decimal optimization yields the loop.
        if any(rt.native_integrity_epoch != epoch for rt, epoch in stream_epochs):
            await self.reject_unsynchronized(key, completed_at)
            return
        evaluation_ms = (elapsed_seconds() - evaluation_started) * 1000
        self.timings.add("route_evaluation_ms", evaluation_ms)
        reasons = [
            *book_reasons(buy, completed_at, self.config),
            *book_reasons(sell, completed_at, self.config),
        ]
        if cad_budget_unavailable:
            reasons.append("BUDGET_NOT_APPLIED_CAD_UNAVAILABLE")
        if rejected_fx:
            reasons.append("FX_CAPTURE_SKEW_EXCEEDED")
        if any(item and item.fresh_until < completed_at for item in (buy_fx, sell_fx)):
            reasons.append("FX_EVIDENCE_EXPIRED_DURING_EVALUATION")
        clock = (
            self.collector.utc_clock.snapshot()
            if self.collector.utc_clock
            else {"trusted": False, "host_healthy": False}
        )
        if not clock["trusted"]:
            reasons.append("UTC_CLOCK_UNTRUSTED")
        if not clock["host_healthy"]:
            reasons.append("HOST_CLOCK_UNHEALTHY")
        capture_uncertainties = []
        for b in (buy, sell):
            try:
                u = float(b.verification.get("clock", {}).get("uncertainty_ms", 2000)) / 1000
                if not 0 <= u <= 2:
                    raise ValueError
            except (ValueError, TypeError):
                u = 2
            capture_uncertainties.append(u)
        uncertainty = sum(capture_uncertainties)
        source_skew = (
            abs((buy.exchange_timestamp - sell.exchange_timestamp).total_seconds())
            if buy.exchange_timestamp and sell.exchange_timestamp
            else None
        )
        received_skew = abs((buy.received_at - sell.received_at).total_seconds())
        if source_skew is None:
            reasons.append("CROSS_BOOK_SOURCE_SYNCHRONIZATION_UNAVAILABLE")
        elif source_skew + uncertainty > self.config.maximum_book_skew_seconds:
            reasons.append("LEG_SOURCE_TIME_SKEW_EXCEEDED")
        if received_skew + uncertainty > self.config.maximum_book_skew_seconds:
            reasons.append("LEG_OBSERVATION_SKEW_WITH_UNCERTAINTY_EXCEEDED")
        collector_config = getattr(self.collector, "config", None)
        if collector_config is not None:
            from arbiq.market_data.timing import freshness

            for b in (buy, sell):
                if freshness(b, completed_at, collector_config, clock)["classification"] in {
                    "STALE",
                    "UNAVAILABLE",
                    "CLOCK_UNTRUSTED",
                }:
                    reasons.append("FRESHNESS_CLASS_NOT_VERIFIABLE")
        source_ages = [age_seconds(completed_at, b.exchange_timestamp) for b in (buy, sell)]
        receive_ages = [age_seconds(completed_at, b.received_at) for b in (buy, sell)]
        if clock.get("trusted"):
            if all(a is not None for a in source_ages):
                source_ms = max(source_ages) * 1000
                self.timings.add("source_to_evaluation_ms", source_ms)
                streamed = sum(b.source.endswith("_ws") for b in (buy, sell))
                transport = ("rest_pair", "mixed_pair", "ws_pair")[streamed]
                self.timings.add(f"source_to_evaluation_{transport}_ms", source_ms)
                if streamed == 2:
                    self.timings.add("ws_pair_source_skew_ms", source_skew * 1000)
            for book, age in zip((buy, sell), source_ages, strict=True):
                if age is not None and book.source.endswith("_ws"):
                    self.timings.add(f"source_to_evaluation_{book.source}_leg_ms", age * 1000)
            if all(a is not None for a in receive_ages):
                self.timings.add("receive_to_evaluation_ms", max(receive_ages) * 1000)
        if (
            abs((buy.received_at - sell.received_at).total_seconds())
            > self.config.maximum_book_skew_seconds
        ):
            reasons.append("LEG_OBSERVATION_SKEW_EXCEEDED")
        reasons.extend(best.reasons if best else result.reasons)
        reasons.extend(
            [
                "CANADIAN_PROVINCIAL_ACCOUNT_ELIGIBILITY_UNVERIFIED",
                "PRE_FUNDED_INVENTORY_ASSUMED_NOT_VERIFIED",
            ]
        )
        if br.health["error_code"] or sr.health["error_code"]:
            reasons.append("EXCHANGE_CONNECTIVITY_DEGRADED")
        status = "RESEARCH_ONLY"
        if best is None:
            reasons.append("NO_FEASIBLE_EXECUTABLE_SIZE")
            status = "INVALIDATED"
        elif best.net_profit_cad is not None and best.net_profit_cad <= 0:
            reasons.append("NON_POSITIVE_NET_AFTER_COSTS")
            status = "INVALIDATED"
        elif best.net_profit_quote is not None and best.net_profit_quote <= 0:
            reasons.append("NON_POSITIVE_NET_AFTER_COSTS")
            status = "INVALIDATED"
        structural = [
            r
            for r in reasons
            if r.startswith(("invalid_", "unordered_"))
            or r
            in {
                "QUOTE_INGESTION_NOT_CURRENT",
                "SOURCE_TIMESTAMP_NOT_CURRENT",
                "BOOK_STALE",
                "BOOK_UNAVAILABLE",
            }
        ]
        if structural:
            status = "INVALIDATED"
        reasons = list(dict.fromkeys(reasons))
        fx_books = (
            fx.referenced_books(buy_fx, sell_fx) if isinstance(fx, ConversionSnapshot) else {}
        )
        payload = {
            "base_asset": bm.base.symbol,
            "buy_exchange": br.slug,
            "sell_exchange": sr.slug,
            "buy_symbol": bm.native_symbol,
            "sell_symbol": sm.native_symbol,
            "status": status,
            "actionable": False,
            "eligibility": "UNVERIFIED",
            "calculation_version": CALCULATION_VERSION,
            "expires_at": now + timedelta(seconds=self.config.route_ttl_seconds),
            "quantity": best.buy_quantity if best else None,
            "buy_price": best.buy_vwap if best else None,
            "sell_price": best.sell_vwap if best else None,
            "net_profit_cad": best.net_profit_cad if best else None,
            "net_return_pct": best.net_return_pct if best else None,
            "acquisition_cost_cad": best.acquisition_cost_cad if best else None,
            "selected_budget_cad": self.config.default_budget_cad,
            "reasons": reasons,
            "supporting_received_at": min(buy.received_at, sell.received_at),
            "supporting_source_at": min(buy.exchange_timestamp, sell.exchange_timestamp)
            if buy.exchange_timestamp is not None and sell.exchange_timestamp is not None
            else None,
            "costs": asdict(best) if best else {},
            "budget_results": [asdict(x) for x in result.budgets],
            "maximum_executable": asdict(result.maximum) if result.maximum else None,
            "optimizer": {
                "evaluated_count": result.evaluated_count,
                "optimality": result.optimality,
                "max_search_evaluations": self.config.max_optimizer_evaluations,
            },
            "evidence": {
                "clock": clock,
                "buy_quality": buy.state.value,
                "sell_quality": sell.state.value,
                "buy_confirmation": buy.verification,
                "sell_confirmation": sell.verification,
                "fee_assumptions": {
                    "buy": buy_fee.as_evidence("buy"),
                    "sell": sell_fee.as_evidence("sell"),
                },
                "fx_books": fx_books,
                "timing": {
                    "evaluation_started_at": now.isoformat(),
                    "evaluated_at": completed_at.isoformat(),
                    "route_evaluation_ms": evaluation_ms,
                    "buy_book_age_seconds": receive_ages[0],
                    "sell_book_age_seconds": receive_ages[1],
                    "buy_source_age_seconds": source_ages[0],
                    "sell_source_age_seconds": source_ages[1],
                    "cross_book_receive_skew_seconds": received_skew,
                    "cross_book_source_skew_seconds": source_skew,
                    "cross_book_uncertainty_seconds": uncertainty,
                    "source_comparison_basis": [
                        b.raw_evidence.get("timestamp_basis") for b in (buy, sell)
                    ],
                    "source_clock_independently_verified": False,
                    "fx_evidence_scope": "used_conversion_books",
                    "fx_evidence_oldest_age_seconds": max(
                        (
                            a
                            for b in fx_books.values()
                            if (
                                a := age_seconds(
                                    completed_at, datetime.fromisoformat(b["received_at"])
                                )
                            )
                            is not None
                        ),
                        default=None,
                    ),
                    "clock_trusted": clock.get("trusted", False),
                    "actionable": False,
                },
                "calculation_inputs": {
                    "buy": self.evidence_pool.market_input(bi),
                    "sell": self.evidence_pool.market_input(si),
                    "costs": safe_json(asdict(costs)),
                    "evaluated_at": now.isoformat(),
                    "max_search_evaluations": self.config.max_optimizer_evaluations,
                },
            },
            "transfer_scenario": {
                "mode": "TRANSFER_SCENARIO",
                "guaranteed_executable": False,
                "reasons": [
                    "NETWORK_COMPATIBILITY_UNVERIFIED",
                    "WITHDRAWAL_COSTS_UNVERIFIED",
                    "TRANSFER_TIMING_UNVERIFIED",
                    "FUTURE_SELLING_PRICE_UNKNOWN",
                ],
                "risk_adjusted_net_cad": None,
            },
        }
        existing = self.current.get(key)
        if existing:
            payload.update(id=existing["id"], detected_at=existing["detected_at"], updated_at=now)
        new = existing is None
        if new and len(self.current) >= self.config.max_candidates:
            terminal = [
                k
                for k, item in self.current.items()
                if item["status"] in {"INVALIDATED", "EXPIRED"}
                and k not in self.pending_transitions
                and time.monotonic() - self.last_seen.get(k, 0) > self.config.route_ttl_seconds
            ]
            if terminal:
                oldest = min(terminal, key=lambda k: self.last_seen.get(k, 0))
                self.current.pop(oldest)
                self.last_seen.pop(oldest, None)
                self.last_write.pop(oldest, None)
            else:
                self.last_seen.pop(key, None)
                self.metrics["candidate_capacity_limited"] += 1
                return
        if key not in self.seen_candidates:
            self.metrics["detected"] += 1
            self.seen_candidates[key] = None
            while len(self.seen_candidates) > self.config.max_candidates * 10:
                self.seen_candidates.popitem(last=False)
        self.metrics["raw_discrepancy_evaluations"] += 1
        rechecked = buy.verification.get("fresh_rest_recheck") and sell.verification.get(
            "fresh_rest_recheck"
        )
        if rechecked:
            payload["evidence"]["both_legs_rechecked"] = True
        if status == "RESEARCH_ONLY" and (new or any(r in ANOMALY_REASONS for r in reasons)):
            for market_id in (buy_id, sell_id):
                if self.collector.request_confirmation(market_id) == "QUEUED":
                    self.metrics["rechecked"] += 1
        now_mono = time.monotonic()
        while self.writes and now_mono - self.writes[0] >= 60:
            self.writes.popleft()
        while self.negative_writes and now_mono - self.negative_writes[0] >= 60:
            self.negative_writes.popleft()
        due = (
            new
            or existing["status"] != status
            or now_mono - self.last_write.get(key, 0) >= self.config.persistence_interval_seconds
        )
        # Reserve most evidence capacity for potentially positive anomalies. Loss
        # examples remain durable, but cannot starve interesting research routes.
        negative_capacity = status != "INVALIDATED" or len(self.negative_writes) < max(
            1, self.config.max_writes_per_minute // 6
        )
        if due and negative_capacity and len(self.writes) < self.config.max_writes_per_minute:
            body = await self.store.save(
                key,
                payload,
                buy,
                sell,
                bm,
                sm,
                br.exchange_id,
                sr.exchange_id,
                bi.fee.rate,
                si.fee.rate,
                now,
            )
            self.current[key] = self.evidence_pool.current(body, payload)
            self.timings.add(
                "opportunity_publication_ms", (elapsed_seconds() - evaluation_started) * 1000
            )
            self.last_write[key] = now_mono
            self.writes.append(now_mono)
            if status == "INVALIDATED":
                self.negative_writes.append(now_mono)
                self.invalidations[reasons[-1]] += 1
        elif existing:
            self.current[key] = self.evidence_pool.current(shared_json(payload), payload)
            if due:
                self.metrics["persistence_rate_limited"] += 1
        elif due:
            self.metrics["persistence_rate_limited"] += 1
            self.last_seen.pop(key, None)

    async def reject_unsynchronized(self, key, now):
        if key in self.current and self.current[key]["status"] not in {"INVALIDATED", "EXPIRED"}:
            await self.invalidate(key, "INVALIDATED", "STREAM_BOOK_UNSYNCHRONIZED", now)
        self.metrics["unsynchronized_pairs_skipped"] += 1

    async def invalidate(self, key, status, reason, now):
        current = self.current[key]
        # Each tracked route owns at most one pending transition. Repeated updates
        # coalesce, while terminal in-memory states cannot lose their audit write.
        self.pending_transitions[key] = (UUID(current["id"]), status, reason, now)
        self.current[key] = {**current, "status": status, "reasons": [reason], "actionable": False}
        self.invalidations[reason] += 1
        await self.flush_transitions()

    async def flush_transitions(self):
        stamp = time.monotonic()
        while self.writes and stamp - self.writes[0] >= 60:
            self.writes.popleft()
        for key in list(self.pending_transitions):
            if len(self.writes) >= self.config.max_writes_per_minute:
                self.metrics["transition_persistence_deferred"] += 1
                break
            await self.store.transition(*self.pending_transitions[key])
            self.pending_transitions.pop(key)
            self.writes.append(stamp)

    def diagnostics(self):
        coverage = coverage_metrics(
            {s: r.catalog for s, r in self.collector.runtimes.items()},
            {s: r.markets for s, r in self.collector.runtimes.items()},
        )
        clock = (
            self.collector.utc_clock.snapshot()
            if self.collector.utc_clock
            else {"trusted": False, "host_healthy": False}
        )
        statuses = Counter(x["status"] for x in self.current.values())
        return {
            "healthy": self.heartbeat > 0 and time.monotonic() - self.heartbeat < 30,
            "enabled": self.config.enabled,
            "mode": "paper",
            "clock_healthy": clock["host_healthy"],
            "clock": clock,
            "metrics": {
                **asdict(coverage),
                **self.metrics,
                "processing_latency_ms": round(sum(self.latencies) / len(self.latencies), 3)
                if self.latencies
                else None,
                "max_processing_latency_ms": round(max(self.latencies), 3)
                if self.latencies
                else None,
                "verified_market": statuses["VERIFIED_MARKET"],
                "research_only": statuses["RESEARCH_ONLY"],
                "invalidated": statuses["INVALIDATED"],
                "expired": statuses["EXPIRED"],
                "pending_assets": len(self.pending),
                "tracked_routes": len(self.current),
                "pending_transitions": len(self.pending_transitions),
                "pending_comparisons": len(self.jobs),
                "comparison_deferred_markets": len(self.comparisons.deferred),
                "comparison_queue_oldest_seconds": max(
                    (elapsed_seconds() - t for t in self.jobs.values() if t is not None), default=0
                ),
                "comparison_deferred_oldest_seconds": max(
                    (
                        elapsed_seconds() - work.queued_at
                        for work in self.comparisons.deferred.values()
                    ),
                    default=0,
                ),
                "pipeline_timings": self.timings.snapshot(),
                "detected_identity_window": len(self.seen_candidates),
                **{
                    f"evidence_pool_{key}": value
                    for key, value in self.evidence_pool.metrics().items()
                },
            },
            "invalidations": dict(self.invalidations),
            "calculation_version": CALCULATION_VERSION,
            "reasons": [
                "All account/province eligibility and pre-funded inventories remain unverified"
            ],
        }

    def schedule_comparisons(self, fx):
        clock = self.collector.utc_clock.snapshot() if self.collector.utc_clock else {}
        context = (id(self.config), clock.get("trusted"), clock.get("host_healthy"))
        requested = set(self.pending_markets)
        self.pending_markets.clear()
        self.pending.clear()
        if self.scheduler_context is not None and self.scheduler_context != context:
            requested.update(self.comparisons.market_keys)
        self.scheduler_context = context
        fx_inputs = {}
        for quote_key, currency in FX_ASSET_KEYS.items():
            if (conversion := fx.get(currency)) is None or currency == "CAD":
                continue
            books = fx.referenced_books(conversion) if isinstance(fx, ConversionSnapshot) else {}
            fx_inputs[quote_key] = (
                conversion.selection_version,
                conversion.verified,
                conversion.fresh_until,
                conversion.observed_at,
                conversion.available_at,
                conversion.oldest_observed_at,
                conversion.oldest_available_at,
                conversion.capture_uncertainty_ms,
                tuple(
                    (leg.evidence, leg.source_currency, leg.target_currency, leg.fee_rate)
                    for leg in (*conversion.to_cad.legs, *conversion.from_cad.legs)
                ),
                tuple(
                    (score["version"], score["applied_taker_rate"], score["applied_fee_currency"])
                    for book in books.values()
                    for score in book["fees"].values()
                ),
            )
        for quote_key in fx_inputs.keys() | self.fx_inputs.keys():
            if fx_inputs.get(quote_key) != self.fx_inputs.get(quote_key):
                requested.update(self.comparisons.by_quote.get(quote_key, ()))
        self.fx_inputs = fx_inputs
        self.metrics["comparison_market_sweeps_coalesced"] += self.comparisons.submit(
            sorted(requested, key=str), elapsed_seconds()
        )

        def accepts(pair):
            quotes = tuple(self.comparisons.market_keys[mid][1] for mid in pair)
            return (
                quotes[0] == quotes[1]
                or all(FX_ASSET_KEYS.get(quote) in fx for quote in quotes)
                or pair in self.last_evaluated  # Revoke a previously evaluated route on loss of FX.
            )

        examined, coalesced, unavailable = self.comparisons.admit(
            self.config.max_candidates * 4, 1024, accepts
        )
        self.metrics["comparison_admission_candidates"] += examined
        self.metrics["comparison_duplicate_jobs_coalesced"] += coalesced
        self.metrics["comparison_unavailable_fx_skipped"] += unavailable
        if self.comparisons.deferred and len(self.jobs) >= self.config.max_candidates * 4:
            self.metrics["comparison_admission_deferrals"] += 1

    async def process_comparisons(self, fx):
        batch_started = time.monotonic()
        processed = 0
        for _ in range(min(512, len(self.jobs))):
            if time.monotonic() - batch_started >= 0.5:
                break
            pair, enqueued = self.jobs.popitem(last=False)
            decision_now = self.collector.utcnow()
            books = tuple(self.collector.cache.get(mid, now=decision_now) for mid in pair)
            clock = self.collector.utc_clock.snapshot() if self.collector.utc_clock else {}
            inputs = (
                tuple(
                    (
                        book.content_sha256,
                        book.exchange_timestamp,
                        book.received_at,
                        book.state,
                        book.reasons,
                        book.raw_evidence.get("timestamp_basis"),
                        book.verification.get("last_error_code"),
                        book.verification.get("clock"),
                    )
                    if book
                    else None
                    for book in books
                ),
                tuple(self.fx_inputs.get(self.comparisons.market_keys[mid][1]) for mid in pair),
                id(self.config),
                tuple(id(self.market_map[mid][1]) for mid in pair),
                clock.get("trusted"),
                clock.get("host_healthy"),
            )
            if self.last_evaluated.get(pair) == inputs:
                self.metrics["identical_comparisons_skipped"] += 1
                continue
            self.last_evaluated[pair] = inputs
            self.last_evaluated.move_to_end(pair)
            while len(self.last_evaluated) > self.config.max_candidates * 4:
                self.last_evaluated.popitem(last=False)
            self.timings.add("comparison_queue_ms", (elapsed_seconds() - enqueued) * 1000)
            await self.evaluate_pair(*pair, fx, decision_now, books=books)
            processed += 1
            if processed % 32 == 0:
                await asyncio.sleep(0)
        self.metrics["comparison_attempts"] += processed

    async def run(self):
        await self.store.expire_previous_runs(self.collector.utcnow())
        while not self.collector.stop.is_set():
            self.wake.clear()
            started = time.monotonic()
            await self.flush_transitions()
            selection = tuple(rt.markets for rt in self.collector.runtimes.values())
            if len(selection) != len(self.selection) or any(
                current is not previous
                for current, previous in zip(selection, self.selection, strict=False)
            ):
                self.rebuild()
            now = self.collector.utcnow()
            if self.config.enabled:
                fx = self.fx_routes(now)
                self.schedule_comparisons(fx)
                await self.process_comparisons(fx)
                for key in list(self.current):
                    if self.current[key]["status"] not in {"INVALIDATED", "EXPIRED"} and (
                        time.monotonic() - self.last_seen.get(key, 0)
                        > self.config.route_ttl_seconds
                    ):
                        await self.invalidate(key, "EXPIRED", "NO_CURRENT_SUPPORTING_EVIDENCE", now)
            self.latencies.append((time.monotonic() - started) * 1000)
            self.heartbeat = time.monotonic()
            waiters = [
                asyncio.create_task(self.wake.wait()),
                asyncio.create_task(self.collector.stop.wait()),
            ]
            try:
                await asyncio.wait(
                    waiters,
                    timeout=(
                        0.05
                        if self.jobs or self.comparisons.deferred
                        else self.config.evaluation_interval_seconds
                    ),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in waiters:
                    task.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
