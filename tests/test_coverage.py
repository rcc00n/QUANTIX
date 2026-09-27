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






