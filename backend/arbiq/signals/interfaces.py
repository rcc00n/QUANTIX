from typing import Protocol
from uuid import UUID


class SignalEvaluator(Protocol):
    """Stage 6: record alert/suppression decision independently from delivery."""

    async def evaluate(self, opportunity_id: UUID, strategy_version_id: UUID) -> UUID: ...
