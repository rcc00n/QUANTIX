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


@pytest.mark.parametrize("offset", [0.001, 1, 80])
def test_future_source_never_has_zero_or_negative_fresh_age(offset):
    future = NOW + timedelta(seconds=offset)
    b = capture(exchange_timestamp=future)
    assert age_seconds(NOW, future) is None
    assert fresh_view(b, NOW, 60).state == DataState.SUSPICIOUS
    assert freshness(b, NOW, CollectorConfig(), CLOCK)["classification"] == "CLOCK_UNTRUSTED"


def test_missing_source_is_never_invented():
    raw = synthetic_raw(NOW)
    raw.pop("timestamp")
    b = synthetic_capture(raw=raw, received_at=NOW)
    result = freshness(b, NOW, CollectorConfig(), CLOCK)
    assert b.exchange_timestamp is None
    assert result["source_age_seconds"] is None
    assert "RECEIPT_AGE_ONLY" in result["reason"]
    assert b.state == DataState.DEGRADED


def test_source_age_continues_to_expire_in_cache():
    b = capture()
    assert fresh_view(b, NOW + timedelta(seconds=61), 60).state == DataState.STALE
    # Preserve a stricter collector-applied policy when API defaults are broader.
    assert (
        freshness(capture(5, state=DataState.STALE), NOW, CollectorConfig(), CLOCK)[
            "classification"
        ]
        == "STALE"
    )


def test_cache_preserves_clock_and_pipeline_in_the_legacy_evidence_envelope():
    book = capture().with_pipeline(collector_processed_at=NOW.isoformat())
    cached = BookCache(CollectorConfig()).put(book)
    assert cached.verification["clock"] == CLOCK
    assert cached.pipeline["collector_processed_at"] == NOW.isoformat()
    payload = cached.model_dump(mode="json")
    assert "pipeline" not in payload  # Old strict CapturedBook reader remains compatible.
    assert payload["verification"]["pipeline"]["collector_processed_at"] == NOW.isoformat()


@pytest.mark.parametrize("out_of_order", ["source", "receipt", "index"])
def test_old_observation_cannot_replace_newer_trusted_cache(out_of_order):
    cache = BookCache(CollectorConfig())
    first = capture(ingestion_index=10)
    first = cache.put(first)
    fields = {"ingestion_index": 11, "received_at": NOW + timedelta(seconds=1)}
    if out_of_order == "source":
        fields["exchange_timestamp"] = NOW - timedelta(seconds=1)
    if out_of_order == "receipt":
        fields["received_at"] = NOW - timedelta(seconds=1)
    if out_of_order == "index":
        fields["ingestion_index"] = 9
    assert cache.put(first.model_copy(update=fields)) is first
    assert sum(cache.rejected_updates.values()) == 1


def test_exact_stream_duplicate_does_not_refresh_receipt_but_rest_can_reobserve():
    cache = BookCache(CollectorConfig())
    b = cache.put(capture(source="kraken_ws"))
    assert cache.put(b.model_copy(update={"received_at": NOW + timedelta(seconds=1)})) is b
    rest = b.model_copy(update={"source": "rest", "received_at": NOW + timedelta(seconds=1)})
    assert cache.put(rest).received_at == rest.received_at


@pytest.mark.parametrize(
    "invalid, source",
    [("source", "rest"), ("receipt", "rest"), ("receipt", "kraken_ws")],
)
def test_cache_recovers_after_untrusted_future_observation(invalid, source):
    cache = BookCache(CollectorConfig())
    fields = {"ingestion_index": 1, "state": DataState.SUSPICIOUS, "source": source}
    if invalid == "source":
        fields.update(
            exchange_timestamp=NOW + timedelta(seconds=80),
            reasons=("invalid_or_future_timestamp",),
        )
    else:
        fields.update(
            received_at=NOW + timedelta(seconds=80),
            verification={"clock": {"trusted": False}},
            reasons=("collector_clock_jump",),
        )
    cache.put(capture(**fields))
    recovered = cache.put(capture(ingestion_index=2, source=source))
    assert recovered.state == DataState.HEALTHY
    assert recovered.received_at == NOW
    assert recovered.exchange_timestamp == NOW
    assert not cache.rejected_updates






























