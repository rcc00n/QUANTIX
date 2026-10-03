"""Stage 8B planning cohort shared by comparison, proposals and offline replay."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from arbiq.opportunities.math import optimize_route
from arbiq.paper_trading.allocation import Cell, PricedAllocation, RouteDemand
from arbiq.paper_trading.funding_sizing import (
    TRADE_SIZES,
    PinnedPair,
    price_size,
    projected_costs,
    read_pair,
    size_at_budget,
)
from arbiq.portfolio.contracts import SCALE, decimal_context, stored
from arbiq.settings.strategy import PaperStrategy


@dataclass(frozen=True, slots=True)
class HistoricalRoute:
    route_id: str
    buy: Cell
    sell: Cell
    evaluation: Any
    frequency: int
    buy_exchange_id: str
    sell_exchange_id: str
    buy_asset_id: str
    sell_asset_id: str
    buy_asset_symbol: str
    sell_asset_symbol: str
    positive_frequency: int | None = None


