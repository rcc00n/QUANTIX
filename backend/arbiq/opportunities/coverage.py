"""Linear catalog indexing; comparisons are limited to selected compatible books."""

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.discovery import DiscoveredMarket, selected_markets


def venue_group(slug: str) -> str:
    return "coinbase" if slug in {"coinbase", "coinbaseexchange"} else slug


