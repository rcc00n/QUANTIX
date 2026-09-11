from typing import Protocol
from uuid import UUID


class PortfolioAccounting(Protocol):
    """Stage 4: atomic, idempotent balanced journal writes under account locks."""

    async def post_journal(self, journal_id: UUID) -> None: ...
