"""SYNTHETIC valuation references. No observed source timestamp may be invented."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from arbiq.opportunities.contracts import (
    ConversionLeg,
    ConversionLevel,
    ConversionRoute,
    FXConversion,
    identity_cad,
)
from arbiq.paper_trading.market import PaperMarkets
from test_paper_execution import NOW, book


def test_missing_source_time_uses_explicit_timestamped_reference_or_remains_unavailable():
    btc, cad, holding, reference, own_id, reference_id = (uuid4() for _ in range(6))
    own = book(own_id, "synthetic-own", NOW).model_copy(update={"exchange_timestamp": None})
    good = book(reference_id, "synthetic-reference", NOW, bid="98", ask="99")
    books = {own_id: own, reference_id: good}
    collector = SimpleNamespace(
        utc_clock=SimpleNamespace(trusted=True, snapshot=lambda: {"trusted": True}),
        scanner=SimpleNamespace(fx_routes=lambda now: {"CAD": identity_cad()}),
        cache=SimpleNamespace(get=lambda mid, **kwargs: books.get(mid)),
    )
    markets = PaperMarkets(collector)
    markets.assets = {
        btc: SimpleNamespace(symbol="BTC", canonical_key="asset:BTC"),
        cad: SimpleNamespace(symbol="CAD", canonical_key="fiat:CAD"),
    }
    markets.markets = {
        own_id: SimpleNamespace(
            id=own_id, exchange_id=holding, base_asset_id=btc, quote_asset_id=cad
        ),
        reference_id: SimpleNamespace(
            id=reference_id,
            exchange_id=reference,
            base_asset_id=btc,
            quote_asset_id=cad,
        ),
    }
    mark = markets.mark(holding, btc, Decimal(1), NOW, research=True)
    assert mark.cad_per_unit == 98
    assert mark.observed_at == NOW
    assert mark.provenance["reference_venue"] is True
    assert mark.verified is False  # Reference cannot prove local executable liquidity.
    books.pop(reference_id)
    assert markets.mark(holding, btc, Decimal(1), NOW, research=True).cad_per_unit is None


def test_crypto_quote_mark_requires_causal_fx_and_preserves_earliest_source():
    btc, eur, holding, mid = (uuid4() for _ in range(4))
    published = NOW - timedelta(seconds=5)
    bid = book(mid, "synthetic-euro-quote", published, bid="98", ask="99")
    fx_published = NOW - timedelta(seconds=12)
    fx_received = NOW - timedelta(seconds=2)
    rate = FXConversion(
        "EUR",
        ConversionRoute(
            (ConversionLeg("EUR", "CAD", (ConversionLevel(Decimal("1.5"), Decimal(1000)),)),)
        ),
        ConversionRoute(
            (ConversionLeg("CAD", "EUR", (ConversionLevel(Decimal("0.66"), Decimal(1000)),)),)
        ),
        False,
        NOW + timedelta(seconds=10),
        "SYNTHETIC two-sided conversion",
        fx_published,
        fx_received,
    )
    collector = SimpleNamespace(
        utc_clock=SimpleNamespace(trusted=True, snapshot=lambda: {"trusted": True}),
        cache=SimpleNamespace(get=lambda mid, **kwargs: bid),
    )
    markets = PaperMarkets(collector)
    markets.assets = {
        btc: SimpleNamespace(symbol="BTC", canonical_key="asset:BTC"),
        eur: SimpleNamespace(symbol="EUR", canonical_key="fiat:EUR"),
    }
    markets.markets = {
        mid: SimpleNamespace(id=mid, exchange_id=holding, base_asset_id=btc, quote_asset_id=eur)
    }
    correct = markets.mark(holding, btc, Decimal(1), NOW, fx={"EUR": rate})
    assert correct.cad_per_unit == 147 and not correct.verified
    assert correct.observed_at == fx_published and correct.received_at == fx_received
    future = markets.mark(
        holding,
        btc,
        Decimal(1),
        NOW,
        fx={"EUR": replace(rate, available_at=NOW + timedelta(seconds=1))},
    )
    assert future.cad_per_unit is None and future.source == "cad_conversion_future"
    unverified_identity = markets.mark(holding, eur, Decimal(1), NOW, fx={"EUR": rate})
    assert unverified_identity.cad_per_unit == Decimal("1.5")
    markets.assets[eur] = SimpleNamespace(symbol="EUR", canonical_key="synthetic:wrapped-eur")
    same_symbol = markets.mark(holding, eur, Decimal(1), NOW, fx={"EUR": rate})
    assert same_symbol.cad_per_unit is None
