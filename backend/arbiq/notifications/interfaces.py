from typing import Protocol
from uuid import UUID


class SignalNotifier(Protocol):
    """Stage 6: persist attempts; deliver only explicitly enabled notifications.

    Tokens and destination IDs are secret configuration, never payload provenance.
    """

    async def notify(self, signal_id: UUID) -> None: ...
