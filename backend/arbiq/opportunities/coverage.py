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


