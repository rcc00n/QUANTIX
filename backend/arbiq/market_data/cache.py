from collections import Counter, OrderedDict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.quality import (
    CapturedBook,
    DataState,
    deviation_pct,
    fresh_view,
    midpoint,
)


def venue_group(exchange: str) -> str:
    return "coinbase" if exchange == "coinbaseexchange" else exchange


