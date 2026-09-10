from typing import Protocol
from uuid import UUID


class PaperExecutor(Protocol):
    """Stage 4: delay, inventory, partial fills and failure evidence; no real orders."""

    async def simulate(self, session_id: UUID, opportunity_id: UUID) -> UUID: ...
