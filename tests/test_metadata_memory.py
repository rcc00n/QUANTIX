"""SYNTHETIC lossless cache tests; no real market data, orders or credentials."""

import asyncio
import pickle
import zlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.connectors.ccxt_public import PublicConnector
from arbiq.connectors.metadata import MetadataArchive, decode, publish


def native(symbol="BTC/CAD", mid="synthetic-btc", base="BTC"):
    return {
        "id": mid,
        "symbol": symbol,
        "base": base,
        "quote": "CAD",
        "baseId": base,
        "quoteId": "CAD",
        "type": "spot",
        "spot": True,
        "contract": False,
        "active": True,
        "settle": None,
        "precision": {"amount": Decimal("0.00000001"), "price": Decimal("0.01")},
        "limits": {
            "amount": {"min": Decimal("0.0001"), "max": None},
            "cost": {"min": Decimal("10.05"), "max": None},
        },
        "taker": Decimal("0.006"),
        "maker": Decimal("-0.0001"),
        "feeSide": "quote",
        "info": {
            "synthetic": True,
            "large": ["public-metadata" * 100] * 5,
            "exact": Decimal("0.123456789012345678"),
            "float_metadata": 0.1,
        },
    }


def test_archive_is_lossless_for_native_metadata_and_dynamic_selection():
    a, b = native(), native("WBTC/CAD", "synthetic-wrapped", "WBTC")
    currencies = {
        "BTC": {"id": "XBT", "code": "BTC", "info": {"name": "Bitcoin"}},
        "CAD": {"id": "ZCAD", "code": "CAD"},
        "WBTC": {"id": "WBTC", "code": "WBTC", "name": "Wrapped Bitcoin"},
    }
    adapter = SimpleNamespace(
        markets={a["symbol"]: a, b["symbol"]: b},
        markets_by_id={a["id"]: [a], b["id"]: [b]},
        currencies=currencies,
        currencies_by_id={x["id"]: x for x in currencies.values()},
    )
    archive = MetadataArchive.capture(adapter)
    assert archive.restore()["markets"] == adapter.markets
    assert archive.compressed_bytes < archive.uncompressed_bytes
    publish(adapter, archive.focused([a["symbol"]]))
    assert adapter.markets == {a["symbol"]: a}
    assert adapter.currencies_by_id["XBT"] == currencies["BTC"]
    assert "WBTC" not in adapter.currencies
    publish(adapter, archive.focused([b["symbol"]]))
    assert adapter.markets == {b["symbol"]: b}
    assert adapter.markets_by_id[b["id"]] == [b]
    assert archive.restore()["markets"][a["symbol"]] == a


async def test_successful_rediscovery_invalidates_the_prior_archive(monkeypatch):
    connector = PublicConnector("kraken", CollectorConfig())
    try:
        a, b = native(), native("ETH/CAD", "synthetic-eth", "ETH")
        connector.adapter.set_markets([a])
        await connector.archive_metadata()
        await connector.focus_metadata([a["symbol"]])
        connector.adapter.set_markets([b])
        monkeypatch.setattr(connector, "_call", AsyncMock(return_value=connector.adapter.markets))
        assert await connector.discover_spot_markets() == list(connector.adapter.markets.values())
        assert connector.metadata_archive is None
        await connector.focus_metadata([b["symbol"]])
        assert set(connector.adapter.markets) == {b["symbol"]}
    finally:
        await connector.close()


async def test_completed_ccxt_load_task_cannot_pin_expanded_market_registry():
    connector = PublicConnector("kraken", CollectorConfig())
    try:
        a, b = native(), native("ETH/CAD", "synthetic-eth", "ETH")
        connector.adapter.set_markets([a, b])
        full = connector.adapter.markets.copy()
        task = asyncio.get_running_loop().create_future()
        task.set_result(connector.adapter.markets)
        connector.adapter.markets_loading = task
        await connector.archive_metadata()
        await connector.focus_metadata([a["symbol"]])
        assert connector.adapter.markets_loading is None
        assert connector.adapter.markets[a["symbol"]] == full[a["symbol"]]
        # CCXT must use its focused cached map, never a hidden new discovery request.
        loaded = await connector.adapter.load_markets()
        assert set(loaded) == {a["symbol"]}
        await connector.focus_metadata([b["symbol"]])
        assert connector.adapter.market(b["symbol"]) == full[b["symbol"]]
        previous = connector.adapter.markets
        with pytest.raises(ValueError, match="absent"):
            await connector.focus_metadata(["MISSING/CAD"])
        assert connector.adapter.markets is previous
    finally:
        await connector.close()




