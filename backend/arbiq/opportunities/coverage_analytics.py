"""Read-only asset coverage over the current bounded runtime selection."""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.cache import venue_group
from arbiq.market_data.discovery import DiscoveredMarket
from arbiq.market_data.quality import CapturedBook, DataState
from arbiq.market_data.timing import distribution, freshness

# Keep explicitly monitored priority assets visible during selection gaps.
PRIORITY_COVERAGE_ASSETS = ("BTC", "ETH", "SOL", "XRP", "LTC", "ADA", "LINK", "DOGE")


@dataclass(slots=True)
class AssetCoverage:
    symbol: str
    verified_identity: bool
    monitored_books: int = 0
    observed_venues: set[str] = field(default_factory=set)
    groups_by_quote: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    transport: Counter = field(default_factory=Counter)
    freshness: Counter = field(default_factory=Counter)
    synchronized_ws_books: int = 0
    retained_route_evaluation_ms: list[float] = field(default_factory=list)
    maximum_executable_buy_cad: Decimal | None = None
    positive_after_cost_routes: int = 0

    def view(self, asset_key: str, routes_available: bool) -> dict:
        return {
            "asset_key": asset_key,
            "asset": self.symbol,
            "verified_identity": self.verified_identity,
            "monitored_books": self.monitored_books,
            "connected_venue_groups": len(self.observed_venues),
            "same_quote_directed_routes": sum(
                len(groups) * (len(groups) - 1) for groups in self.groups_by_quote.values()
            ),
            "transport": dict(self.transport),
            "freshness": dict(self.freshness),
            "synchronized_ws_books": self.synchronized_ws_books,
            "retained_route_evaluation_ms": distribution(self.retained_route_evaluation_ms),
            "maximum_executable_buy_cad": str(self.maximum_executable_buy_cad)
            if self.maximum_executable_buy_cad is not None
            else None,
            "positive_after_cost_routes": self.positive_after_cost_routes
            if routes_available
            else None,
        }


def _record_route(row: AssetCoverage, route: dict) -> None:
    evidence = route.get("evidence") or {}
    timing = evidence.get("timing") or {}
    duration = timing.get("route_evaluation_ms")
    if duration is not None:
        row.retained_route_evaluation_ms.append(duration)
    if route["status"] not in {"RESEARCH_ONLY", "VERIFIED_MARKET", "RECHECKING"}:
        return
    profit = route.get("net_profit_cad")
    if profit is not None and Decimal(profit) > 0:
        row.positive_after_cost_routes += 1
    maximum = (route.get("maximum_executable") or {}).get("acquisition_cost_cad")
    if maximum is not None:
        value = Decimal(maximum)
        if row.maximum_executable_buy_cad is None or value > row.maximum_executable_buy_cad:
            row.maximum_executable_buy_cad = value


def asset_coverage(
    monitored: Mapping[str, Sequence[DiscoveredMarket]],
    books: Iterable[CapturedBook],
    synchronized_ws_ids: Set[UUID],
    routes: Iterable[dict] | None,
    *,
    config: CollectorConfig,
    now: datetime,
    clock: dict,
) -> list[dict]:
    """Count canonical venue directions and distinguish book coverage from route evidence."""
    by_id = {book.market_id: book for book in books}
    rows = {f"asset:{symbol}": AssetCoverage(symbol, True) for symbol in PRIORITY_COVERAGE_ASSETS}
    selected = {}
    for slug, markets in monitored.items():
        group = venue_group(slug)
        for market in markets:
            base = market.base
            if base.key not in rows:
                rows[base.key] = AssetCoverage(base.symbol, base.verified)
            row = rows[base.key]
            row.monitored_books += 1
            if market.active is True and base.verified and market.quote.verified:
                row.groups_by_quote[market.quote.key].add(group)
                selected[slug, market.native_symbol] = base.key

            book = by_id.get(market.id)
            if book is None:
                row.transport["unavailable"] += 1
                row.freshness["UNAVAILABLE"] += 1
                continue
            streamed = book.source.endswith("_ws")
            row.transport["websocket" if streamed else "rest"] += 1
            state = freshness(book, now, config, clock)["classification"]
            row.freshness[state] += 1
            if book.state in {DataState.HEALTHY, DataState.DEGRADED}:
                if state in {"FRESH", "AGING"}:
                    row.observed_venues.add(group)
            if (
                streamed
                and book.market_id in synchronized_ws_ids
                and book.state in {DataState.HEALTHY, DataState.DEGRADED, DataState.STALE}
                and "stream_unsynchronized" not in book.reasons
            ):
                row.synchronized_ws_books += 1

    routes_available = routes is not None and bool(clock.get("trusted"))
    for route in routes if routes_available else ():
        # Scanner projections contain one latest evaluation per direction, even when
        # the opportunity is rejected. A retired market cannot contribute evidence.
        if (expires := route.get("expires_at")) is None or datetime.fromisoformat(expires) <= now:
            continue
        buy_asset = selected.get((route.get("buy_exchange"), route.get("buy_symbol")))
        sell_asset = selected.get((route.get("sell_exchange"), route.get("sell_symbol")))
        if buy_asset is not None and buy_asset == sell_asset:
            _record_route(rows[buy_asset], route)
    return [
        row.view(key, routes_available)
        for key, row in sorted(rows.items(), key=lambda item: (item[1].symbol, item[0]))
    ]
