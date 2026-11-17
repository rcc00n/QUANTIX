"""SYNTHETIC directed depth and clock windows; no public market result is implied."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from arbiq.opportunities.analysis import dump_input, load_input
from arbiq.opportunities.contracts import (
    ConversionLeg,
    ConversionLevel,
    Fee,
    MarketInput,
    MarketRules,
)
from arbiq.opportunities.conversions import (
    ConversionSnapshot,
    FXBook,
    select_cad_paths,
    spot_conversion_leg,
    synchronized_conversion,
)
from arbiq.opportunities.math import convert, required_input

D = Decimal
NOW = datetime(2026, 7, 1, tzinfo=UTC)


def depth(price, quantity):
    return (ConversionLevel(D(price), D(quantity)),)


def edge(base, quote, bid, reverse, *, size="1000", seconds=1, uncertainty=0, ref=None):
    reference = ref or f"{base}-{quote}:synthetic"
    stamp = NOW - timedelta(seconds=seconds)
    return FXBook(
        ConversionLeg(base, quote, depth(bid, size), D("0.01"), reference),
        ConversionLeg(quote, base, depth(reverse, size), D("0.01"), reference),
        stamp,
        stamp,
        uncertainty,
    )


def paths(*edges):
    return select_cad_paths(
        edges,
        now=NOW,
        age=timedelta(seconds=30),
        skew=timedelta(seconds=5),
        probe_cad=D(500),
    )


def test_three_hops_preserve_stablecoin_deviation_bid_ask_and_depth():
    valid = [
        edge("USDT", "USD", "0.97", "0.99"),
        edge("USD", "EUR", "0.9", "1.1"),
        edge("EUR", "CAD", "1.5", "0.65"),
    ]
    shallow = edge("USDT", "CAD", "1.48", "0.65", size="7")
    result = paths(*valid, shallow)["USDT"]
    assert result == paths(shallow, *reversed(valid))["USDT"]
    assert [leg.target_currency for leg in result.to_cad.legs] == ["USD", "EUR", "CAD"]
    assert (
        convert(result.to_cad, D(10))
        == D(10) * D("0.97") * D("0.9") * D("1.5") * D("0.99") ** 3
    )
    assert required_input(result.from_cad, D(100)) > D(100)
    assert not result.verified
    assert result.oldest_available_at == NOW - timedelta(seconds=1)


def test_causal_time_windows_reject_stale_future_and_excessive_capture_uncertainty():
    early = edge("USDC", "USD", "0.97", "0.99", seconds=5, uncertainty=1000)
    late = edge("USD", "CAD", "1.3", "0.73", seconds=1, uncertainty=1000)
    future = edge("USDC", "CAD", "1.24", "0.78", seconds=-1)
    stale = edge("USDC", "CAD", "1.24", "0.78", seconds=31)
    result = paths(early, late, future, stale)
    assert set(result) == {"CAD", "USD"}
    valid = result["USD"]
    assert synchronized_conversion(valid, NOW, NOW, 100, timedelta(seconds=5))
    assert not synchronized_conversion(
        valid, NOW - timedelta(seconds=6), NOW, 100, timedelta(seconds=5)
    )
    assert not synchronized_conversion(valid, None, NOW, 0, timedelta(seconds=5))


def test_source_currency_commission_is_charged_in_source_units_without_extrapolation():
    from arbiq.market_data.contracts import BookLevel

    fee = SimpleNamespace(currency=lambda _side: "quote", applied_taker_rate=D("0.02"))
    asks = (BookLevel(price=D(2), quantity=D(100)),)
    bids = (BookLevel(price=D(1), quantity=D(100)),)
    acquired = spot_conversion_leg(
        "EUR", "CAD", bids=bids, asks=asks, reference="synthetic", fee=fee, side="buy"
    )
    sold = spot_conversion_leg(
        "EUR", "CAD", bids=bids, asks=asks, reference="synthetic", fee=fee, side="sell"
    )
    assert acquired.levels[0].quantity == D(204)
    assert abs(acquired.levels[0].price * D(204) - D(100)) < D("1e-22")
    assert sold.levels[0].quantity == D(100) and sold.fee_rate == D("0.02")


def test_roundtrip_and_snapshot_pin_only_used_conversion_books():
    from arbiq.market_data.contracts import BookLevel

    rates = paths(edge("EUR", "CAD", "1.4", "0.69"), edge("USD", "CAD", "1.3", "0.73"))
    snapshot = ConversionSnapshot(
        rates, {"EUR-CAD": {"id": "eur"}, "USD-CAD": {"id": "usd"}}
    )
    assert snapshot.referenced_books(snapshot["EUR"]) == {"EUR-CAD": {"id": "eur"}}
    market = MarketInput(
        "market",
        "synthetic",
        "BTC",
        "EUR",
        (BookLevel(price=D(2), quantity=D(1)),),
        (BookLevel(price=D(1), quantity=D(1)),),
        Fee(D("0.01")),
        MarketRules(),
        snapshot["EUR"],
    )
    assert load_input(dump_input(market)) == market
