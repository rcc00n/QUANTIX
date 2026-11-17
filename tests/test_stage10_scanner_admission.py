"""SYNTHETIC scheduler stress without exchange/network or storage side effects."""

from collections import defaultdict
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from arbiq.opportunities.comparison_queue import ComparisonQueue
from test_arbitrage_engine import NOW, capture, engine_fixture, market


def selection():
    by_base = defaultdict(set)
    by_market = {}
    definitions = (
        ("BTC", "coinbase"),
        ("BTC", "coinbaseexchange"),
        ("BTC", "kraken"),
        ("BTC", "ndax"),
        ("DOGE", "coinbaseexchange"),
        ("DOGE", "kraken"),
    )
    for index, (asset, venue) in enumerate(definitions, 1):
        mid = UUID(int=index)
        mx = SimpleNamespace(
            base=SimpleNamespace(key=asset), quote=SimpleNamespace(key="fiat:CAD")
        )
        by_market[mid] = SimpleNamespace(slug=venue), mx
        by_base[asset].add(mid)
    q = ComparisonQueue()
    q.rebuild(by_market, by_base, 0)
    return q, tuple(sorted(by_market))


def test_one_changed_book_only_schedules_its_own_routes_and_excludes_aliases():
    q, (coinbase, alias, kraken, ndax, _, _) = selection()
    q.submit([coinbase], 1)
    q.admit(20, 20, lambda _: True)
    assert set(q.ready) == {
        (coinbase, kraken),
        (kraken, coinbase),
        (coinbase, ndax),
        (ndax, coinbase),
    }
    assert all(alias not in pair for pair in q.ready)
    assert not q.deferred


def test_queue_cap_retains_deferrals_and_rotates_to_quiet_asset():
    q, (first, _, _, _, quiet, quiet_peer) = selection()
    q.submit([first, quiet], 1)
    observed = []
    for tick in range(2, 15):
        q.submit([first], tick)  # Continuous BTC updates cannot starve DOGE.
        q.admit(1, 3, lambda _: True)
        assert len(q.ready) <= 1
        assert len(q.deferred) <= len(q.market_keys)
        if q.ready:
            (pair, _) = q.ready.popitem(last=False)
            observed.append(pair)
    assert (quiet, quiet_peer) in observed[:4]
    assert (quiet_peer, quiet) in observed[:5]
    assert len({pair for pair in observed if quiet not in pair}) == 4


def test_fx_path_reuse_ends_when_capture_expires():
    engine, _, _, _ = engine_fixture()
    usd = market("synthetic-buy", base="USD", quote="CAD")
    runtime = engine.collector.runtimes["synthetic-buy"]
    runtime.markets.append(usd)
    engine.collector.cache.books[usd.id] = capture("synthetic-buy", usd)
    engine.rebuild()
    initial = engine.fx_routes(NOW)
    assert "USD" in initial
    assert engine.fx_routes(NOW + timedelta(seconds=1)) is initial
    assert set(engine.fx_routes(NOW + timedelta(seconds=31))) == {"CAD"}
    assert engine.metrics["fx_graph_reuses"] == 1
    assert engine.metrics["fx_graph_rebuilds"] == 2
