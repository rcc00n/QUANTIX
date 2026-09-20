"""Linear catalog indexing; comparisons are limited to selected compatible books."""

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.discovery import DiscoveredMarket, selected_markets


def venue_group(slug: str) -> str:
    return "coinbase" if slug in {"coinbase", "coinbaseexchange"} else slug


def intersections(catalogs: dict[str, list[DiscoveredMarket]]) -> dict[tuple[str, str], set[str]]:
    coverage = defaultdict(set)
    for slug, markets in catalogs.items():
        for market in markets:
            if market.active is not False and market.base.verified and market.quote.verified:
                coverage[(market.base.key, market.quote.key)].add(venue_group(slug))
    return dict(coverage)


def allocate(
    catalogs: dict[str, list[DiscoveredMarket]],
    config: CollectorConfig,
    relevance: dict[tuple[str, str], int] | None = None,
    quality: dict[tuple[str, str], str] | None = None,
):
    """Prefer intersections, retaining conversion anchors and configured filters.

    Liquid same-quote intersections outrank single-venue listings. Two CAD currency
    books, where present, anchor directional CAD conversion without stablecoin parity.
    All allocations respect the per-exchange cap; no unbounded subscriptions.
    """
    coverage = intersections(catalogs)
    result = {}
    relevance = relevance or {}
    quality = quality or {}
    for slug, markets in catalogs.items():
        # Reuse Stage 2 filters without its small final cap.
        eligible = selected_markets(
            markets, config.model_copy(update={"max_markets_per_exchange": len(markets)})
        )

        def rank(m, slug=slug):
            count = len(coverage.get((m.base.key, m.quote.key), set()))
            return (
                0 if config.prefer_cad and m.quote.symbol == "CAD" else 1,
                0 if m.base.symbol in {"USD", "USDC", "USDT"} and m.quote.symbol == "CAD" else 1,
                0 if count > 1 else 1,
                config.priority_assets.index(m.base.symbol)
                if m.base.symbol in config.priority_assets
                else 999,
                -min(relevance.get((m.base.key, m.quote.key), 0), 10),
                {"HEALTHY": 0, "SUSPICIOUS": 1, "DEGRADED": 2, "STALE": 3, "UNAVAILABLE": 4}.get(
                    quality.get((slug, m.native_symbol)), 1
                ),
                -count,
                config.quote_assets.index(m.quote.symbol)
                if m.quote.symbol in config.quote_assets
                else 999,
                -(m.quote_volume or 0),
                m.native_symbol,
            )

        ordered = sorted(eligible, key=rank)
        result[slug] = ordered[: config.max_markets_per_exchange]
    return result


