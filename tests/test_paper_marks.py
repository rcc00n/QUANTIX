"""SYNTHETIC valuation references. No observed source timestamp may be invented."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import test_paper_api as paper_fixtures
from arbiq.db.models import Asset, Exchange
from arbiq.opportunities.contracts import (
    ConversionLeg,
    ConversionLevel,
    ConversionRoute,
    FXConversion,
    identity_cad,
)
from arbiq.paper_trading.coordinator import PaperCoordinator
from arbiq.paper_trading.market import PaperMarkets
from arbiq.paper_trading.service import Allocation, create_portfolio
from arbiq.portfolio import valuation
from arbiq.portfolio.models import PortfolioCheckpoint
from arbiq.settings.strategy import strategy_lab_settings
from sqlalchemy import select
from test_paper_execution import NOW, book

private_api = paper_fixtures.private_api


def test_missing_source_time_uses_explicit_timestamped_reference_or_remains_unavailable():
    btc, cad, holding, reference, own_id, reference_id = (uuid4() for _ in range(6))
    own = book(own_id, "synthetic-own", NOW).model_copy(
        update={"exchange_timestamp": None}
    )
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
    assert (
        markets.mark(holding, btc, Decimal(1), NOW, research=True).cad_per_unit is None
    )


def test_crypto_quote_mark_requires_causal_fx_and_preserves_earliest_source():
    btc, eur, holding, mid = (uuid4() for _ in range(4))
    published = NOW - timedelta(seconds=5)
    bid = book(mid, "synthetic-euro-quote", published, bid="98", ask="99")
    fx_published = NOW - timedelta(seconds=12)
    fx_received = NOW - timedelta(seconds=2)
    rate = FXConversion(
        "EUR",
        ConversionRoute(
            (
                ConversionLeg(
                    "EUR", "CAD", (ConversionLevel(Decimal("1.5"), Decimal(1000)),)
                ),
            )
        ),
        ConversionRoute(
            (
                ConversionLeg(
                    "CAD", "EUR", (ConversionLevel(Decimal("0.66"), Decimal(1000)),)
                ),
            )
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
        mid: SimpleNamespace(
            id=mid, exchange_id=holding, base_asset_id=btc, quote_asset_id=eur
        )
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
    markets.assets[eur] = SimpleNamespace(
        symbol="EUR", canonical_key="synthetic:wrapped-eur"
    )
    same_symbol = markets.mark(holding, eur, Decimal(1), NOW, fx={"EUR": rate})
    assert same_symbol.cad_per_unit is None


@pytest.mark.asyncio
async def test_four_checkpoint_marks_share_unchanging_books_and_publish_together(
    private_api, monkeypatch
):
    """SYNTHETIC market writes between awaited checkpoint inserts must have no effect."""
    client, dbs, asset_key = private_api
    owner = UUID(client.get("/api/v1/auth/me").json()["id"])
    async with dbs() as db:
        cad = await db.scalar(select(Asset).where(Asset.canonical_key == "fiat:CAD"))
        asset = await db.scalar(select(Asset).where(Asset.canonical_key == asset_key))
        venue = await db.scalar(select(Exchange).where(Exchange.slug == "kraken"))
        ids = []
        for profile in (
            "LIQUID_ARBITRAGE",
            "ANOMALY_HUNTER",
            "HYBRID_OPTIMIZER",
            "NO_TRADING",
        ):
            session, _ = await create_portfolio(
                db,
                owner,
                "SYNTHETIC " + profile,
                strategy_lab_settings(profile),
                Decimal(10000),
                [
                    Allocation(exchange="kraken", quantity="9900"),
                    Allocation(
                        exchange="kraken", asset=asset_key, quantity="1", cad_rate="100"
                    ),
                ],
                NOW - timedelta(seconds=5),
                passive=profile == "NO_TRADING",
            )
            ids.append(session.id)
        await db.commit()
    eur = SimpleNamespace(id=uuid4(), canonical_key="fiat:EUR")
    mid = uuid4()
    frames = {
        mid: book(
            mid, "synthetic-frame", NOW - timedelta(seconds=1), bid="98", ask="99"
        )
    }
    initial_hash = frames[mid].content_sha256
    observations = {"book_reads": 0, "fx_reads": 0, "converted": Decimal("1.5")}

    def market_book(key, *, now):
        assert now == NOW
        observations["book_reads"] += 1
        return frames.get(key)

    def fx_routes(at):
        assert at == NOW
        observations["fx_reads"] += 1
        conversion = FXConversion(
            "EUR",
            ConversionRoute(
                (
                    ConversionLeg(
                        "EUR",
                        "CAD",
                        (ConversionLevel(observations["converted"], Decimal(1000)),),
                    ),
                )
            ),
            ConversionRoute(
                (
                    ConversionLeg(
                        "CAD", "EUR", (ConversionLevel(Decimal("0.66"), Decimal(1000)),)
                    ),
                )
            ),
            False,
            NOW + timedelta(seconds=1),
            "SYNTHETIC pinned frame",
            NOW - timedelta(seconds=2),
            NOW - timedelta(seconds=1),
        )
        return {"EUR": conversion, "CAD": identity_cad()}

    collector = SimpleNamespace(
        utcnow=lambda: NOW,
        utc_clock=SimpleNamespace(trusted=True, snapshot=lambda: {"trusted": True}),
        scanner=SimpleNamespace(fx_routes=fx_routes),
        cache=SimpleNamespace(get=market_book),
    )
    market = PaperMarkets(collector)
    market.assets = {asset.id: asset, cad.id: cad, eur.id: eur}
    market.markets = {
        mid: SimpleNamespace(
            id=mid, exchange_id=venue.id, base_asset_id=asset.id, quote_asset_id=eur.id
        )
    }
    owner_checkpoints = select(PortfolioCheckpoint).where(
        PortfolioCheckpoint.session_id.in_(ids)
    )
    record = valuation.snapshot
    shared = []

    async def snapshot_for_test(
        db, session_id, prices, at, *, research_only, benchmark_prices
    ):
        item = await record(
            db,
            session_id,
            prices,
            at,
            research_only=research_only,
            benchmark_prices=benchmark_prices,
        )
        if session_id in ids:
            assert (
                prices[(venue.id, asset.id)] is benchmark_prices[(venue.id, asset.id)]
            )
            shared.append(prices[(venue.id, asset.id)])
            async with dbs() as reader:
                assert not list(await reader.scalars(owner_checkpoints))
            # Simulate the collector replacing its live inputs during this awaited write.
            frames[mid] = book(mid, "synthetic-frame", NOW, bid="101", ask="102")
            observations["converted"] = Decimal(2)
        return item

    monkeypatch.setattr(valuation, "snapshot", snapshot_for_test)
    hour = NOW.astimezone(ZoneInfo("America/Edmonton")).strftime("%Y-%m-%dT%H%z")
    worker = SimpleNamespace(
        sessions=dbs,
        market=market,
        collector=collector,
        daily_hours=dict.fromkeys(ids, hour),
    )
    await PaperCoordinator.checkpoint_portfolios(worker)
    assert len(shared) == 4 and all(item is shared[0] for item in shared)
    assert observations["book_reads"] == observations["fx_reads"] == 1
    async with dbs() as db:
        rows = list(await db.scalars(owner_checkpoints))
    assert len(rows) == 4
    assert {row.as_of for row in rows} == {NOW}
    for row in rows:
        assert Decimal(row.payload["equity_cad"]) == Decimal(10047)
        assert row.payload["equity_cad"] == row.payload["no_trading_benchmark_cad"]
        evidence = next(
            row for row in row.payload["balances"] if row["asset_id"] == str(asset.id)
        )
        assert (
            evidence["price_evidence"]["provenance"]["content_sha256"] == initial_hash
        )
