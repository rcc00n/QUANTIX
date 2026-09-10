from typing import Protocol
from uuid import UUID

from arbiq.market_data.contracts import NormalizedOrderBook


class OpportunityDetector(Protocol):
    """Stage 3: depth, freshness, fees, FX and expenses must all be evaluated."""

    async def evaluate(
        self, buy: NormalizedOrderBook, sell: NormalizedOrderBook
    ) -> UUID | None: ...
