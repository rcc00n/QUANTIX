"""SYNTHETIC exchange catalogs; never evidence of live connectivity."""

from dataclasses import replace
from decimal import Decimal

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.discovery import normalize_market
from arbiq.opportunities.coverage import allocate, intersections, metrics


def market(venue, base="BTC", quote="USDT"):
    return normalize_market(
        venue,
        {
            "id": base + quote,
            "symbol": f"{base}/{quote}",
            "base": base,
            "quote": quote,
            "spot": True,
            "active": True,
            "precision": {"amount": Decimal("0.001")},
        },
        {},
        precision_mode=4,
    )


def test_aliases_and_identity_do_not_create_false_intersections():
    catalogs = {v: [market(v)] for v in ("coinbase", "coinbaseexchange", "kraken")}
    assert len(intersections(catalogs)[("asset:BTC", "asset:USDT")]) == 2
    assert metrics(catalogs, catalogs).compatible_intersections == 1
    catalogs["kraken"] = [market("kraken", "WBTC")]
    assert metrics(catalogs, catalogs).compatible_intersections == 0


def test_allocation_respects_filters_capacity_and_conversion_anchors():
    catalogs = {
        v: [
            market(v, "BTC", "CAD"),
            market(v, "USDC", "CAD"),
            market(v, "ETH"),
            market(v, "SOL"),
        ]
        for v in ("kraken", "ndax")
    }
    selected = allocate(catalogs, CollectorConfig(max_markets_per_exchange=2))
    assert all(len(x) == 2 for x in selected.values())
    assert all(any(m.base.symbol == "USDC" for m in x) for x in selected.values())
    selected = allocate(
        catalogs, CollectorConfig(max_markets_per_exchange=2, base_assets=["ETH"])
    )
    assert all([m.base.symbol for m in x] == ["ETH"] for x in selected.values())
    assert metrics(catalogs, selected).directed_comparisons == 2


def test_inactive_and_unverified_assets_do_not_count():
    m = market("a")
    assert (
        metrics(
            {"a": [m], "b": [replace(m, active=False)]},
            {"a": [m], "b": [replace(m, active=False)]},
        ).compatible_intersections
        == 0
    )


def test_recent_quality_and_research_relevance_influence_quote_allocation():
    catalogs = {
        v: [market(v, "BTC", "USDT"), market(v, "BTC", "USDC")]
        for v in ("kraken", "ndax")
    }
    config = CollectorConfig(max_markets_per_exchange=1)
    selected = allocate(
        catalogs, config, quality={(v, "BTC/USDT"): "STALE" for v in catalogs}
    )
    assert all(items[0].quote.symbol == "USDC" for items in selected.values())
    selected = allocate(catalogs, config, relevance={("asset:BTC", "asset:USDT"): 3})
    assert all(items[0].quote.symbol == "USDT" for items in selected.values())
