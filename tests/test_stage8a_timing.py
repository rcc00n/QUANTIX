"""SYNTHETIC timestamps/books only. These tests make no external API calls."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.collector.worker import Collector, ExchangeRuntime
from arbiq.market_data.cache import BookCache
from arbiq.market_data.quality import DataState, fresh_view
from arbiq.market_data.timing import Timings, age_seconds, distribution, freshness
from arbiq.opportunities.contracts import identity_cad
from test_arbitrage_engine import engine_fixture
from test_collector_recovery import settings
from test_market_data import synthetic_capture, synthetic_raw

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CLOCK = {"trusted": True, "host_healthy": True, "uncertainty_ms": "10"}


def capture(age=0, **updates):
    return synthetic_capture(received_at=NOW - timedelta(seconds=age)).model_copy(
        update={
            "verification": {"clock": CLOCK},
            **updates,
        }
    )


@pytest.mark.parametrize("age, expected", [(0, "FRESH"), (3, "AGING"), (31, "STALE")])
def test_explicit_freshness_boundaries(age, expected):
    assert freshness(capture(age), NOW, CollectorConfig(), CLOCK)["classification"] == expected


@pytest.mark.parametrize("trusted", [False, None])
def test_untrusted_clock_never_establishes_freshness(trusted):
    assert (
        freshness(capture(), NOW, CollectorConfig(), {"trusted": trusted})["classification"]
        == "CLOCK_UNTRUSTED"
    )












































