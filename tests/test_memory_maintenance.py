"""Synthetic lifecycle checks; no market data or financial state is created."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from arbiq.core.memory import maintain_process_memory, reclaim_unused_pages


def test_reclaim_reports_only_process_memory():
    result = reclaim_unused_pages()
    assert result["elapsed_ms"] >= 0
    assert (
        result["unused_pages_released_bytes"] is None
        or result["unused_pages_released_bytes"] >= 0
    )
    assert "unreachable objects" in result["scope"]


@pytest.mark.asyncio
async def test_maintenance_is_bounded_and_cancellable():
    state = SimpleNamespace(memory_maintenance=None)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    async def run(function):
        return {"scope": "SYNTHETIC unreachable objects only"}

    with (
        patch("arbiq.core.memory.asyncio.sleep", sleep),
        patch("arbiq.core.memory.asyncio.to_thread", run),
        pytest.raises(asyncio.CancelledError),
    ):
        await maintain_process_memory(state, interval_seconds=300)
    assert sleeps == [60, 300]
    assert state.memory_maintenance["scope"].startswith("SYNTHETIC")
