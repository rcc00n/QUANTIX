"""SYNTHETIC catalogs and depth. Profile probes never enter financial history."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from arbiq.collector.config import CollectorConfig, MarketProfile
from arbiq.collector.stream_selection import native_symbols
from arbiq.opportunities.coverage import allocate, intersections, quote_liquidity_ranks
from arbiq.settings.terminal import MarketPreferences
from pydantic import ValidationError
from test_coverage import market

ASSETS = (
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "LTC",
    "ADA",
    "LINK",
    "DOGE",
    "BCH",
    "DOT",
    "AVAX",
    "XLM",
)


def catalogs(venues, assets=ASSETS, quotes=("USDT", "USD")):
    return {
        venue: [
            replace(
                market(venue, base, quote),
                id=uuid4(),
                quote_volume=Decimal(100_000 - index * 1000)
                if quote == "USDT"
                else Decimal(100),
            )
            for index, base in enumerate(assets)
            for quote in quotes
        ]
        for venue in venues
    }


@pytest.mark.parametrize("profile", tuple(MarketProfile))
def test_profiles_are_deterministic_and_keep_the_same_book_cap(profile):
    rows = catalogs(("kraken", "ndax", "coinbase", "coinbaseexchange"))
    rows["kraken"].append(market("kraken", "USDC", "CAD"))
    config = CollectorConfig(max_markets_per_exchange=9, market_profile=profile)
    first = allocate(rows, config, research_epoch=3)
    shuffled = {
        venue: list(reversed(value)) for venue, value in reversed(list(rows.items()))
    }
    second = allocate(shuffled, config, research_epoch=3)
    assert first == second
    assert all(len(books) == 9 for books in first.values())
    assert any(m.native_symbol == "USDC/CAD" for m in first["kraken"])
    assert all(len({m.base.key for m in books}) >= 5 for books in first.values())
    assert all(
        "coinbaseexchange" not in groups for groups in intersections(first).values()
    )


def test_broad_profile_probes_smaller_assets_without_relevance_lock_in():
    rows = catalogs(("kraken", "ndax"))
    config = CollectorConfig(
        max_markets_per_exchange=10, market_profile=MarketProfile.BROAD_EXPLORATION
    )
    relevance = {("asset:BTC", "asset:USDT"): 999_999}
    snapshots = [
        allocate(rows, config, relevance, research_epoch=turn) for turn in range(32)
    ]
    assert all(len({m.base.key for m in snap["kraken"]}) == 10 for snap in snapshots)
    assert {m.base.symbol for snap in snapshots for m in snap["kraken"]} == set(ASSETS)
    assert all(
        sum(m.base.symbol == "BTC" for m in snap["kraken"]) == 1 for snap in snapshots
    )


def test_liquid_profile_uses_observed_executable_native_depth_within_quote_currency():
    rows = catalogs(("kraken", "ndax"), assets=("SOL", "ETH", "BTC"), quotes=("USDT",))
    config = CollectorConfig(
        max_markets_per_exchange=1, market_profile=MarketProfile.LIQUID_MARKETS
    )
    keys = {
        ("kraken", "ETH/USDT"): Decimal(1_000),
        ("kraken", "SOL/USDT"): Decimal(10_000),
        ("kraken", "BTC/USDT"): Decimal(50_000),
    }
    chosen = allocate(
        rows, config, depth=keys, quality={("kraken", "BTC/USDT"): "STALE"}
    )
    assert [m.native_symbol for m in chosen["kraken"]] == ["SOL/USDT"]


def test_liquidity_ranks_do_not_compare_numerical_volume_across_assets():
    rows = [
        replace(market("kraken", base, quote), quote_volume=Decimal(volume))
        for base, quote, volume in (
            ("SOL", "CAD", "50000"),
            ("ETH", "CAD", "30000"),
            ("SOL", "BTC", "0.9"),
            ("ETH", "BTC", "0.8"),
        )
    ]
    ranks = quote_liquidity_ranks(rows, lambda m: m.quote_volume)
    assert ranks["SOL/CAD"] == ranks["SOL/BTC"] == 0
    assert ranks["ETH/CAD"] == ranks["ETH/BTC"] == 500


def test_profile_preserves_allowlists_and_market_order_applicability():
    rows = catalogs(("kraken", "ndax"), assets=("BTC", "ETH", "SOL"), quotes=("USDT",))
    blocked = rows["kraken"][0]
    rows["kraken"][0] = replace(
        blocked,
        metadata={
            **blocked.metadata,
            "trading_rules": {"market_order_available": False},
        },
    )
    config = CollectorConfig(
        max_markets_per_exchange=1,
        base_assets=["asset:BTC", "asset:SOL"],
        market_profile=MarketProfile.BALANCED_ADAPTIVE,
    )
    chosen = allocate(rows, config)
    assert {m.base.symbol for m in chosen["kraken"]} == {"SOL"}
    assert {m.base.symbol for m in chosen["ndax"]} == {"SOL"}


def test_profiled_websockets_preserve_healthy_subscriptions_and_exchange_limits():
    monitored = catalogs(
        ("okx", "kraken"), assets=("BTC", "ETH", "SOL", "XRP"), quotes=("USDT",)
    )
    sol = next(m for m in monitored["okx"] if m.base.symbol == "SOL")
    retained = {sol.native_id: sol}
    options = {
        "profile": MarketProfile.BALANCED_ADAPTIVE,
        "retained": retained,
        "depth": {
            ("okx", "BTC/USDT"): Decimal(1000),
            ("okx", "SOL/USDT"): Decimal(2),
        },
    }
    priorities = ["BTC", "ETH", "SOL", "XRP"]
    assert set(native_symbols("okx", monitored, priorities, 1, **options)) == {
        sol.native_id
    }
    quiet = {("okx", "SOL/USDT"): "STALE"}
    assert set(
        native_symbols("okx", monitored, priorities, 1, quality=quiet, **options)
    ) == {sol.native_id}
    bad_quality = {("okx", "SOL/USDT"): "UNAVAILABLE"}
    expected = next(m for m in monitored["okx"] if m.base.symbol == "BTC")
    assert set(
        native_symbols("okx", monitored, priorities, 1, quality=bad_quality, **options)
    ) == {expected.native_id}
    assert native_symbols("okx", monitored, priorities, 0, **options) == {}
    assert len(monitored["okx"]) == 4


def test_cold_profiled_native_slots_preserve_first_four_independent_priority_assets():
    priorities = ["BTC", "ETH", "SOL", "XRP"]
    monitored = catalogs(
        ("coinbaseexchange", "okx", "kraken"), assets=priorities, quotes=("USDT",)
    )
    selected = {
        slug: {
            m.base.symbol
            for m in native_symbols(
                slug,
                monitored,
                priorities,
                2,
                profile=MarketProfile.BALANCED_ADAPTIVE,
            ).values()
        }
        for slug in ("coinbaseexchange", "okx")
    }
    assert selected == {"coinbaseexchange": {"BTC", "ETH"}, "okx": {"SOL", "XRP"}}


def test_terminal_defaults_and_validation_preserve_old_preferences():
    assert MarketPreferences().market_profile is None
    assert CollectorConfig().market_profile is None
    updated = MarketPreferences(market_profile="BALANCED_ADAPTIVE")
    assert updated.market_profile == MarketProfile.BALANCED_ADAPTIVE
    assert updated.model_dump(mode="json")["market_profile"] == "BALANCED_ADAPTIVE"
    with pytest.raises(ValidationError):
        MarketPreferences(market_profile="UNBOUNDED")
