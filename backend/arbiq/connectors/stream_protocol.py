"""Shared contract for native public depth protocols."""

from dataclasses import dataclass
from typing import Protocol


class StreamInvalid(ValueError):
    """A feed must obtain new snapshots before using its depth again."""


@dataclass(frozen=True, slots=True)
class StreamFrame:
    books: tuple[tuple[str, dict], ...] = ()
    synchronized: frozenset[str] = frozenset()
    activity: bool = True
    pong: bool = False


class DepthProtocol(Protocol):
    url: str
    ping_message: str | None

    def subscription(self) -> dict: ...

    def consume(self, message: str | bytes) -> StreamFrame: ...
