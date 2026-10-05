"""Entirely SYNTHETIC deterministic paper execution; never live exchange verification."""

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.db.models import (
    ArbitrageEvaluation,
    Asset,
    CollectorRun,
    Exchange,
    Journal,
    Market,
    PaperFill,
    PaperOrder,
    PaperTrade,
    User,
)
from arbiq.market_data.cache import BookCache
from arbiq.market_data.contracts import BookLevel
from arbiq.market_data.discovery import normalize_market
from arbiq.market_data.quality import validate_book
from arbiq.opportunities.analysis import dump_input
from arbiq.opportunities.contracts import (
    Costs,
    Fee,
    MarketInput,
    MarketRules,
    identity_cad,
)
from arbiq.opportunities.math import optimize_route
from arbiq.opportunities.storage import OpportunityStore
from arbiq.paper_trading.config import PaperConfig
from arbiq.paper_trading.coordinator import PaperCoordinator, allowed_paper_asset
from arbiq.paper_trading.execution import (
    evidence_eligibility,
    planned_reserves,
    recheck_projection,
    simulate_leg,
)
from arbiq.paper_trading.models import PaperControl
from arbiq.paper_trading.service import Allocation, create_portfolio
from arbiq.portfolio.accounting import reconcile
from arbiq.portfolio.models import PortfolioPosition
from arbiq.settings.strategy import PaperStrategy, TransferPolicy
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 22, 5, tzinfo=UTC)
D = Decimal


def book(mid, slug, now, bid="99", ask="100", qty="100", index=1):
    return validate_book(
        {
            "symbol": "BTC/CAD",
            "timestamp": int(now.timestamp() * 1000),
            "bids": [[D(bid), D(qty)], [D(bid) - 1, D(qty)], [D(bid) - 2, D(qty)]],
            "asks": [[D(ask), D(qty)], [D(ask) + 1, D(qty)], [D(ask) + 2, D(qty)]],
        },
        market_id=mid,
        exchange=slug,
        symbol="BTC/CAD",
        base_key="asset:BTC",
        quote_key="fiat:CAD",
        received_at=now,
        request_started_at=now,
        latency_ms=1,
        config=CollectorConfig(),
        source="SYNTHETIC",
        identity_verified=True,
        active_verified=True,
        clock_healthy=True,
    ).model_copy(update={"ingestion_index": index})


def inputs(b, venue):
    return MarketInput(
        str(b.market_id),
        venue,
        "asset:BTC",
        "CAD",
        b.normalized.asks,
        b.normalized.bids,
        Fee(D(".001"), verified=False),
        MarketRules(D(".1"), D(".1"), D("1"), verified=True),
        identity_cad(),
    )


def test_owner_slippage_cap_rejects_deep_buy_and_sell_but_preserves_exact_boundary():
    # SYNTHETIC three-level depth; this is a policy limit, not a real fill.
    b = book(uuid4(), "synthetic-slip", NOW, bid="99", ask="100", qty="1")
    rules = MarketRules(D(".1"), D(".01"), D(".1"), verified=True)
    for side in ("buy", "sell"):
        with pytest.raises(ValueError, match="MAXIMUM_SLIPPAGE_EXCEEDED"):
            simulate_leg(
                b,
                side,
                D("2"),
                D("1000"),
                Fee(D(0)),
                rules,
                slippage_bps=D(10),
                maximum_slippage_bps=D(20),
            )
    fill = simulate_leg(
        b,
        "buy",
        D("1"),
        D("1000"),
        Fee(D(0)),
        rules,
        slippage_bps=D(10),
        maximum_slippage_bps=D(10),
    )
    assert fill.quantity == D(1)
    assert fill.modeled_price == D("100.1")
    with pytest.raises(ValueError, match="maximum acceptable"):
        PaperStrategy(slippage_bps="11", maximum_slippage_bps="10")


def test_planned_inventory_matches_execution_rounding_for_long_precise_units():
    # SYNTHETIC18-place inventory, eight-place prices and nonzero taker/slippage.
    q = D("0.123456789012345678")
    b = book(uuid4(), "synthetic-precise", NOW, bid="99.12345678", ask="100.12345678")
    rules = MarketRules(amount_step=D("1e-18"), verified=True)
    buy_fee, sell_fee = Fee(D(".006"), "quote"), Fee(D(".006"), "base")
    fill = simulate_leg(b, "buy", q, D(10000), buy_fee, rules, D(10))
    buy, base = planned_reserves(
        SimpleNamespace(buy_notional=fill.observed_notional, sell_quantity=q),
        buy_fee,
        sell_fee,
        D(10),
    )
    assert buy == fill.quote_notional + fill.fee_quantity
    exact_buy = simulate_leg(b, "buy", q, buy, buy_fee, rules, D(10))
    assert exact_buy.quantity == q
    sell = simulate_leg(b, "sell", q, base, sell_fee, rules, D(10))
    assert sell.quantity == q
    assert base == sell.quantity + sell.fee_quantity
    assert buy.as_tuple().exponent >= -18 and base.as_tuple().exponent >= -18


def test_buy_sizes_on_the_grid_of_rounded_principal_and_fee_together():
    # SYNTHETIC accounting boundary. The old unrounded affordability inverse
    # sized 0.4, then charged 3 units against a reserve of 2 units.
    b = book(uuid4(), "synthetic-double-rounding", NOW)
    b = b.model_copy(
        update={
            "normalized": b.normalized.model_copy(
                update={
                    "asks": (BookLevel(price="0.000000000000000003", quantity="1"),),
                    "bids": (BookLevel(price="0.000000000000000001", quantity="1"),),
                }
            )
        }
    )
    fill = simulate_leg(
        b, "buy", D("0.4"), D("2e-18"), Fee(D("0.5")), MarketRules(amount_step=D("0.1"))
    )
    assert fill.partial and fill.quantity == D("0.3")
    assert fill.quote_notional == fill.fee_quantity == D("1e-18")


def test_stage8b_canonical_research_filter_does_not_accept_same_symbol_wrapped_assets():
    assert allowed_paper_asset(("asset:BTC",), "BTC", "asset:BTC")
    assert not allowed_paper_asset(("asset:BTC",), "BTC", "synthetic:wrapped-btc")
    assert not allowed_paper_asset(("asset:BTC",), "BTC", None)
    assert allowed_paper_asset(("BTC",), "BTC", None)  # Pinned legacy strategies.


def strategy(**updates):
    return PaperStrategy(
        mode="RESEARCH_SHADOW",
        preset="Custom",
        reaction_delay_seconds=5,
        minimum_profit_cad="0",
        minimum_profit_pct="0",
        minimum_liquidity_cad="1",
        trade_amount_cad="250",
        maximum_capital_per_exchange_pct="100",
        maximum_inventory_exposure_pct="100",
        maximum_loss_cad="1000",
        taker_fee_rate=".001",
        slippage_bps="0",
        rebalance_bps="0",
        **updates,
    )


def test_single_leg_depth_precision_fees_and_insufficient_inventory():
    b = book(uuid4(), "synthetic", NOW, qty="1")
    r = MarketRules(D(".1"), D(".1"), D("1"), max_notional=D("101"), verified=True)
    f = simulate_leg(b, "buy", D("5"), D("1000"), Fee(D(".01")), r, D("100"))
    assert f.quantity <= D("1") and f.quote_notional <= 101 and f.partial
    with pytest.raises(ValueError):
        simulate_leg(b, "sell", D(1), D(0), Fee(D(".01")), r)
    with pytest.raises(ValueError):
        simulate_leg(b, "buy", 1.1, D(10), Fee(D(".01")), r)
    base = simulate_leg(b, "sell", D(1), D(1), Fee(D(".01"), "base"), r)
    assert base.quantity + base.fee_quantity <= 1


def test_causal_timing_clock_and_changed_spread():
    buy = book(uuid4(), "synthetic-buy", NOW)
    sell = book(uuid4(), "synthetic-sell", NOW, bid="110", ask="111")
    assert (
        evidence_eligibility(
            buy,
            now=NOW - timedelta(seconds=1),
            eligible_at=NOW - timedelta(seconds=2),
            maximum_age=30,
            research=True,
        )[0]
        is False
    )
    assert (
        evidence_eligibility(
            buy,
            now=NOW,
            eligible_at=NOW,
            maximum_age=30,
            research=True,
            clock_trusted=False,
        )[0]
        is False
    )
    result = recheck_projection(
        buy_book=buy,
        sell_book=sell,
        buy_input=inputs(buy, "a"),
        sell_input=inputs(sell, "b"),
        planned_quantity=D(1),
        decision_at=NOW,
        strategy=strategy(),
    )
    assert result.net_profit_cad > 0
    gone = book(buy.market_id, "synthetic-buy", NOW, bid="119", ask="120")
    with pytest.raises(ValueError, match="SPREAD_DISAPPEARED"):
        recheck_projection(
            buy_book=gone,
            sell_book=sell,
            buy_input=inputs(buy, "a"),
            sell_input=inputs(sell, "b"),
            planned_quantity=D(1),
            decision_at=NOW,
            strategy=strategy(),
        )
    with pytest.raises(ValueError, match="NO_CAUSAL_OPPOSITE_BOOK"):
        recheck_projection(
            buy_book=buy,
            sell_book=None,
            buy_input=inputs(buy, "a"),
            sell_input=inputs(sell, "b"),
            planned_quantity=D(1),
            decision_at=NOW,
            strategy=strategy(),
        )


@pytest.fixture
async def environment(database_url, monkeypatch, request):
    e = create_async_engine(database_url)
    sessions = async_sessionmaker(e, expire_on_commit=False)
    state = {"now": NOW, "raw": 100.0}
    monkeypatch.setattr("arbiq.paper_trading.coordinator.elapsed_seconds", lambda: state["raw"])
    suffix = uuid4().hex[:12]
    async with sessions() as db:
        cad = await db.scalar(select(Asset).where(Asset.canonical_key == "fiat:CAD"))
        if not cad:
            cad = Asset(
                canonical_key="fiat:CAD",
                symbol="CAD",
                kind="fiat",
                identifiers={"identity_verified": True},
            )
            db.add(cad)
            await db.flush()
        btc = Asset(
            canonical_key="synthetic:" + suffix,
            symbol="BTC",
            kind="crypto",
            identifiers={"synthetic": True},
        )
        user = User(username="synthetic-paper-" + suffix, password_hash="SYNTHETIC-not-auth")
        bx = Exchange(slug="synthetic-buy-" + suffix, name="SYNTHETIC BUY")
        sx = Exchange(slug="synthetic-sell-" + suffix, name="SYNTHETIC SELL")
        run = CollectorRun(
            worker_id="synthetic-" + suffix,
            status="running",
            heartbeat_at=NOW,
            config_snapshot={"synthetic": True},
            metrics={},
        )
        db.add_all([btc, user, bx, sx, run])
        await db.flush()
        bm = Market(
            exchange_id=bx.id,
            base_asset_id=btc.id,
            quote_asset_id=cad.id,
            native_symbol="BTC/CAD",
            active=True,
            amount_step=D(".1"),
            price_tick=D("1"),
            min_notional=D("1"),
            metadata_json={},
        )
        sm = Market(
            exchange_id=sx.id,
            base_asset_id=btc.id,
            quote_asset_id=cad.id,
            native_symbol="BTC/CAD",
            active=True,
            amount_step=D(".1"),
            price_tick=D("1"),
            min_notional=D("1"),
            metadata_json={},
        )
        db.add_all([bm, sm])
        await db.flush()
        transfer_mode = getattr(request, "param", None) == "transfer"
        config = strategy(
            research_execution_model="TRANSFER" if transfer_mode else "PRE_FUNDED",
            transfer=TransferPolicy(
                network="SYNTHETIC_BTC",
                withdrawal_enabled=True,
                deposit_enabled=True,
                withdrawal_fee_base=".1",
                settlement_seconds=10,
                haircut_pct="0",
            ),
        )
        if getattr(request, "param", None) == "default_slippage":
            config = config.model_copy(update={"slippage_bps": D("10")})
        if getattr(request, "param", None) == "small_size":
            # This isolated synthetic experiment deliberately studies sub-C$50
            # independent-leg partial fills, rather than qualifying alert size.
            config = config.model_copy(update={"min_buy_capital_cad": D("0")})
        session, control = await create_portfolio(
            db,
            user.id,
            "SYNTHETIC paper",
            config,
            D(10000),
            (
                [Allocation(exchange=bx.slug, quantity="10000")]
                if transfer_mode
                else [
                    Allocation(exchange=bx.slug, quantity="5000"),
                    Allocation(
                        exchange=sx.slug,
                        asset=btc.canonical_key,
                        quantity="50",
                        cad_rate="100",
                    ),
                ]
            ),
            NOW,
        )
        await db.commit()
        ids = {
            "bm": bm.id,
            "sm": sm.id,
            "bx": bx.id,
            "sx": sx.id,
            "btc": btc.id,
            "cad": cad.id,
            "session": session.id,
            "control": control.session_id,
            "user": user.id,
        }
    cache = BookCache(CollectorConfig(exchange_ids=[bx.slug, sx.slug]))
    buy = book(bm.id, bx.slug, NOW)
    sell = book(sm.id, sx.slug, NOW, bid="110", ask="111")
    cache.put(buy)
    cache.put(sell)
    collector = SimpleNamespace(
        settings=SimpleNamespace(
            paper=PaperConfig(bootstrap_portfolios=False), admin_username="synthetic"
        ),
        store=SimpleNamespace(sessions=sessions, run_id=run.id),
        cache=cache,
        scanner=SimpleNamespace(fx_routes=lambda now: {"CAD": identity_cad()}),
        monitored_ids=lambda: {bm.id, sm.id},
        runtimes={},
        utc_clock=SimpleNamespace(
            trusted=True,
            snapshot=lambda: {
                "trusted": True,
                "host_healthy": False,
                "utc_now": state["now"].isoformat(),
                "uncertainty_ms": 0,
            },
        ),
        utcnow=lambda: state["now"],
        request_confirmation=lambda mid: "QUEUED",
        stop=asyncio.Event(),
    )
    coord = PaperCoordinator(collector)
    async with sessions() as db:
        await coord.market.refresh(db)
        mi = []
        for mid, slug in ((bm.id, bx.slug), (sm.id, sx.slug)):
            m = normalize_market(
                slug,
                {
                    "id": "synthetic",
                    "symbol": "BTC/CAD",
                    "base": "BTC",
                    "quote": "CAD",
                    "spot": True,
                    "active": True,
                    "precision": {"amount": D(".1"), "price": D("1")},
                    "limits": {"amount": {"min": D(".1")}, "cost": {"min": D("1")}},
                },
                {},
                precision_mode=4,
            )
            mi.append(replace(m, id=mid))
        bi, si = inputs(buy, bx.slug), inputs(sell, sx.slug)
        result = optimize_route(
            bi, si, budget_cad=D(250), now=NOW, costs=Costs(D(0), D(0)), budgets=()
        )
        payload = {
            "base_asset": "BTC",
            "buy_exchange": bx.slug,
            "sell_exchange": sx.slug,
            "buy_symbol": "BTC/CAD",
            "sell_symbol": "BTC/CAD",
            "status": "RESEARCH_ONLY",
            "actionable": False,
            "reasons": ["SYNTHETIC"],
            "expires_at": NOW + timedelta(seconds=120),
            "quantity": result.best.buy_quantity,
            "acquisition_cost_cad": result.best.acquisition_cost_cad,
            "net_profit_cad": result.best.net_profit_cad,
            "net_return_pct": result.best.net_return_pct,
            "costs": asdict(result.best),
            "evidence": {
                "synthetic": True,
                "calculation_inputs": {
                    "buy": dump_input(bi),
                    "sell": dump_input(si),
                    "costs": dump_input(Costs(D(0), D(0))),
                    "evaluated_at": NOW.isoformat(),
                },
            },
        }
        await OpportunityStore(sessions, run.id).save(
            "synthetic-" + suffix,
            payload,
            buy,
            sell,
            mi[0],
            mi[1],
            bx.id,
            sx.id,
            D(".001"),
            D(".001"),
            NOW,
        )
        evaluation = await db.scalar(
            select(ArbitrageEvaluation)
            .where(ArbitrageEvaluation.buy_snapshot_id.is_not(None))
            .order_by(ArbitrageEvaluation.created_at.desc())
            .limit(1)
        )
        # Look up by exact market route to avoid neighboring synthetic fixtures.
        from arbiq.db.models import ArbitrageRoute

        route = await db.scalar(select(ArbitrageRoute).where(ArbitrageRoute.buy_market_id == bm.id))
        evaluation = await db.scalar(
            select(ArbitrageEvaluation).where(ArbitrageEvaluation.route_id == route.id)
        )
        await coord.schedule(
            db,
            await db.get(PaperControl, session.id),
            await db.get(
                __import__("arbiq.db.models", fromlist=["PaperSession"]).PaperSession,
                session.id,
            ),
            route,
            evaluation,
            NOW - timedelta(seconds=1)
            if getattr(request, "param", None) == "future_evaluation"
            else NOW,
        )
        await db.commit()
        await coord.refresh_targets(db)
        trade = await db.scalar(select(PaperTrade).where(PaperTrade.session_id == session.id))
        ids["trade"] = trade.id
    yield SimpleNamespace(
        engine=e,
        sessions=sessions,
        state=state,
        ids=ids,
        coordinator=coord,
        collector=collector,
        buy=buy,
        sell=sell,
    )
    await e.dispose()


async def at(env, seconds):
    env.state.update(now=NOW + timedelta(seconds=seconds), raw=100 + seconds)


async def advance(env):
    async with env.sessions() as db:
        t = await db.get(PaperTrade, env.ids["trade"])
        await env.coordinator.advance(db, t, env.state["now"])
        await db.commit()
        await env.coordinator.refresh_targets(db)


@pytest.mark.parametrize("environment", ["future_evaluation"], indirect=True)
async def test_future_dated_evaluation_never_reserves_or_orders(environment):
    e = environment
    async with e.sessions() as db:
        trade = await db.get(PaperTrade, e.ids["trade"])
        assert trade.status == "MISSED"
        assert trade.reason == "EVALUATION_NOT_AVAILABLE_AT_SCHEDULING_TIME"
        assert await db.scalar(select(PaperOrder.id).where(PaperOrder.trade_id == trade.id)) is None
        assert await reconcile(db, trade.session_id)


@pytest.mark.parametrize("environment", [None, "default_slippage"], indirect=True)
async def test_delayed_independent_execution_idempotency_and_ledger(environment):
    e = environment
    async with e.sessions() as db:
        t = await db.get(PaperTrade, e.ids["trade"])
        assert t.status == "SCHEDULED"
    await at(e, 4)
    await e.coordinator.observe(book(e.buy.market_id, e.buy.exchange, e.state["now"], index=3))
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=4,
        )
    )
    await at(e, 6)
    await e.coordinator.observe(book(e.buy.market_id, e.buy.exchange, e.state["now"], index=5))
    await advance(e)
    async with e.sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(PaperFill)
                .join(PaperOrder)
                .where(PaperOrder.trade_id == e.ids["trade"])
            )
            == 1
        )
    await at(e, 7)
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=6,
        )
    )
    await advance(e)
    async with e.sessions() as db:
        t = await db.get(PaperTrade, e.ids["trade"])
        assert t.status == "COMPLETED"
        assert D(t.state["result"]["net_pnl_cad"]) > 0
        assert await reconcile(db, e.ids["session"])
        before = await db.scalar(select(func.count()).select_from(Journal))
    await advance(e)
    async with e.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Journal)) == before


async def test_one_sided_timeout_preserves_exposure(environment):
    e = environment
    await at(e, 4)
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=3,
        )
    )
    await at(e, 6)
    await e.coordinator.observe(book(e.buy.market_id, e.buy.exchange, e.state["now"], index=4))
    await advance(e)
    await at(e, 100)
    await advance(e)
    async with e.sessions() as db:
        t = await db.get(PaperTrade, e.ids["trade"])
        assert t.status == "PARTIAL"
        assert t.state["exposure"]["unmatched"] and t.state["result"] == {}
        p = await db.scalar(
            select(PortfolioPosition).where(
                PortfolioPosition.session_id == e.ids["session"],
                PortfolioPosition.exchange_id == e.ids["bx"],
                PortfolioPosition.asset_id == e.ids["btc"],
            )
        )
        assert p.quantity > 0 and await reconcile(db, e.ids["session"])


async def test_spread_disappearance_never_fabricates_fills(environment):
    e = environment
    await at(e, 4)
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=3,
        )
    )
    await at(e, 6)
    await e.coordinator.observe(
        book(
            e.buy.market_id,
            e.buy.exchange,
            e.state["now"],
            bid="119",
            ask="120",
            index=4,
        )
    )
    await advance(e)
    async with e.sessions() as db:
        fills = await db.scalar(
            select(func.count())
            .select_from(PaperFill)
            .join(PaperOrder)
            .where(PaperOrder.trade_id == e.ids["trade"])
        )
        assert fills == 0


@pytest.mark.parametrize("environment", ["small_size"], indirect=True)
async def test_partial_independent_legs_never_count_as_closed_profit(environment):
    e = environment
    await at(e, 4)
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=3,
        )
    )
    await at(e, 6)
    await e.coordinator.observe(
        book(e.buy.market_id, e.buy.exchange, e.state["now"], qty=".1", index=4)
    )
    await advance(e)
    await at(e, 7)
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=5,
        )
    )
    await advance(e)
    async with e.sessions() as db:
        trade = await db.get(PaperTrade, e.ids["trade"])
        assert trade.status == "PARTIAL"
        assert trade.state["result"] == {} and trade.state["exposure"]["unmatched"]
        assert await reconcile(db, e.ids["session"])










