"""Synthetic asset coverage fixtures and current-route samples."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.discovery import normalize_market
from arbiq.market_data.quality import DataState
from arbiq.opportunities.coverage_analytics import asset_coverage
from test_market_data import synthetic_capture, synthetic_market

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CLOCK = {"trusted": True, "uncertainty_ms": 0}


def listing(exchange, base="BTC", quote="USD", *, wrapped=False):
    metadata = {base: {"name": "wrapped token"}} if wrapped else {}
    return replace(
        normalize_market(exchange, synthetic_market(base, quote), metadata), id=uuid4()
    )


def book(exchange, market, seconds, source="rest"):
    return synthetic_capture(
        market_id=market.id,
        exchange=exchange,
        received_at=NOW - timedelta(seconds=seconds),
        source=source,
    ).model_copy(update={"verification": {"clock": CLOCK}})


def coverage(markets, books=(), synchronized=(), routes=(), clock=CLOCK):
    values = asset_coverage(
        markets,
        books,
        set(synchronized),
        routes,
        now=NOW,
        clock=clock,
        config=CollectorConfig(),
    )
    return {row["asset_key"]: row for row in values}


def test_catalog_compatibility_respects_aliases_quote_identity_and_unknown_assets():
    rows = coverage(
        {
            "coinbase": [listing("coinbase")],
            "coinbaseexchange": [listing("coinbaseexchange")],
            "synthetic-other": [
                listing("synthetic-other"),
                listing("synthetic-other", quote="USDT"),
                listing("synthetic-other", wrapped=True),
            ],
        }
    )
    assert rows["asset:BTC"]["monitored_books"] == 4
    assert rows["asset:BTC"]["same_quote_directed_routes"] == 2
    assert rows["asset:BTC"]["transport"] == {"unavailable": 4}
    assert rows["exchange:synthetic-other:BTC"]["verified_identity"] is False
    assert rows["exchange:synthetic-other:BTC"]["same_quote_directed_routes"] == 0
    assert rows["asset:LINK"]["monitored_books"] == 0


def test_websocket_synchronization_remains_separate_from_quiet_book_age():
    native = listing("synthetic-native")
    rest = listing("synthetic-rest")
    quiet = book("synthetic-native", native, 36, "synthetic_native_ws").classified(
        DataState.STALE
    )
    current = book("synthetic-rest", rest, 1)
    markets = {"synthetic-native": [native], "synthetic-rest": [rest]}
    result = coverage(markets, [quiet, current], [native.id])["asset:BTC"]
    assert result["transport"] == {"websocket": 1, "rest": 1}
    assert result["freshness"] == {"STALE": 1, "FRESH": 1}
    assert result["synchronized_ws_books"] == 1
    assert result["connected_venue_groups"] == 1
    quarantined = quiet.classified(DataState.STALE, reasons=("stream_unsynchronized",))
    result = coverage(markets, [quarantined, current], [native.id])["asset:BTC"]
    assert result["synchronized_ws_books"] == 0


def test_scanner_route_samples_exclude_expired_unselected_and_nonpositive_evidence():
    markets = {
        slug: [listing(slug, base="SOL")]
        for slug in ("synthetic-buy", "synthetic-sell")
    }
    current = {
        "buy_exchange": "synthetic-buy",
        "sell_exchange": "synthetic-sell",
        "buy_symbol": "SOL/USD",
        "sell_symbol": "SOL/USD",
        "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
        "status": "RESEARCH_ONLY",
        "net_profit_cad": "0.000000000000000001",
        "maximum_executable": {"acquisition_cost_cad": "2500.001000000000000001"},
        "evidence": {"timing": {"route_evaluation_ms": 2.5}},
    }
    rejected = {**current, "status": "INVALIDATED"}
    expired = {**current, "expires_at": NOW.isoformat()}
    unselected = {**current, "sell_exchange": "synthetic-retired"}
    route_rows = (current, rejected, expired, unselected)
    result = coverage(markets, routes=route_rows)["asset:SOL"]
    assert result["positive_after_cost_routes"] == 1
    assert result["maximum_executable_buy_cad"] == "2500.001000000000000001"
    assert result["retained_route_evaluation_ms"]["sample_count"] == 2
    assert result["retained_route_evaluation_ms"]["p95"] is None
    untrusted = coverage(markets, routes=route_rows, clock={"trusted": False})[
        "asset:SOL"
    ]
    assert untrusted["positive_after_cost_routes"] is None
    assert untrusted["maximum_executable_buy_cad"] is None
