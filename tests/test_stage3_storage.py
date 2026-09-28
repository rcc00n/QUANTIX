"""Isolated PostgreSQL integration; all prices, venues and evidence are SYNTHETIC."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.collector.storage import CollectorStore
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    FeeVersion,
    OrderBookSnapshot,
)
from arbiq.opportunities.analysis import resize
from arbiq.opportunities.contracts import CALCULATION_VERSION, identity_cad
from arbiq.opportunities.storage import OpportunityStore
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_arbitrage_engine import NOW, SyntheticCache, capture, engine_fixture


async def seed_scanner(database_url):
    database = create_async_engine(database_url)
    sessions = async_sessionmaker(database, expire_on_commit=False)
    store = CollectorStore(sessions, CollectorConfig())
    await store.start(f"synthetic-scanner-{uuid4()}")
    scanner, _, _, _ = engine_fixture(
        buy_slug=f"synthetic-buy-{uuid4().hex}",
        sell_slug=f"synthetic-sell-{uuid4().hex}",
    )
    books = []
    for index, rt in enumerate(scanner.collector.runtimes.values()):
        rt.exchange_id = await store.register_exchange(rt.slug, "SYNTHETIC venue", {})
        rt.markets = await store.save_markets(rt.exchange_id, rt.markets, rt.markets)
        rt.catalog = rt.markets
        books.append(
            capture(
                rt.slug,
                rt.markets[0],
                ask="100" if index == 0 else "111",
                bid="99" if index == 0 else "110",
            )
        )
    scanner.collector.cache = SyntheticCache(books)
    scanner.rebuild()
    return database, sessions, store, scanner, books


@pytest.mark.asyncio
async def test_pinned_revisions_replay_lifecycle_restart_and_append_only_guards(
    database_url,
):
    database, sessions, collector_store, scanner, books = await seed_scanner(
        database_url
    )
    try:
        buy_id, sell_id = books[0].market_id, books[1].market_id
        await scanner.evaluate_pair(buy_id, sell_id, {"CAD": identity_cad()}, NOW)
        key, payload, buy, sell = scanner.store.saved[0]
        # SyntheticStore records JSON, whereas the engine→store boundary uses UTC datetimes.
        payload = {
            **payload,
            "expires_at": datetime.fromisoformat(payload["expires_at"]),
        }
        buy_rt, buy_market = scanner.market_map[buy_id]
        sell_rt, sell_market = scanner.market_map[sell_id]
        storage = OpportunityStore(sessions, collector_store.run_id)

        async def save(body=payload):
            return await storage.save(
                key,
                body,
                buy,
                sell,
                buy_market,
                sell_market,
                buy_rt.exchange_id,
                sell_rt.exchange_id,
                scanner.fee_rate(buy_market),
                scanner.fee_rate(sell_market),
                NOW,
            )

        # A rejected financial amount rolls back snapshots, fees and route together.
        # Retrying the same store must not retain IDs of rolled-back fee versions.
        with pytest.raises(ValueError, match="NUMERIC"):
            await save({**payload, "net_profit_cad": "1e50"})
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(FeeVersion)
                    .where(FeeVersion.market_id.in_([buy_id, sell_id]))
                )
                == 0
            )
        body = await save()
        again = await save()
        route_id = UUID(body["id"])
        assert again["id"] == body["id"]
        async with sessions() as db:
            route = await db.get(ArbitrageRoute, route_id)
            assert route.revision == 2 and route.actionable is False
            revisions = (
                await db.scalars(
                    select(ArbitrageEvaluation)
                    .where(ArbitrageEvaluation.route_id == route_id)
                    .order_by(ArbitrageEvaluation.revision)
                )
            ).all()
            assert [r.revision for r in revisions] == [1, 2]
            assert all(r.calculation_version == CALCULATION_VERSION for r in revisions)
            assert revisions[0].buy_snapshot_id == revisions[1].buy_snapshot_id
            assert revisions[0].buy_fee_version_id == revisions[1].buy_fee_version_id
            evidence = revisions[0].evidence
            assert evidence["buy_book"]["content_sha256"] == buy.content_sha256
            assert datetime.fromisoformat(evidence["buy_book"]["received_at"]) == NOW
            assert evidence["buy_market_metadata"]["minimum_quantity"] == "0.001"
            assert evidence["clock"]["trusted"] is True
            assert revisions[0].quantity == Decimal(body["quantity"])
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(OrderBookSnapshot)
                    .where(OrderBookSnapshot.market_id.in_([buy_id, sell_id]))
                )
                == 2
            )
            fee = await db.get(FeeVersion, revisions[0].buy_fee_version_id)
            assert fee.assumptions["verified"] is False and fee.taker_rate > 0

        replay = resize(body, Decimal(1000))
        assert replay["net_profit_cad"] == body["net_profit_cad"]
        assert replay["quantity"] == body["quantity"]
        assert "original evaluation time" in replay["budget_recalculation"]

        for table in ("arbitrage_evaluations", "arbitrage_lifecycle_events"):
            for sql in (
                f"UPDATE {table} SET created_at = created_at WHERE route_id = :route_id",
                f"DELETE FROM {table} WHERE route_id = :route_id",
                f"TRUNCATE {table}",
            ):
                with pytest.raises(
                    DBAPIError,
                    match="append-only|cannot truncate a table referenced"
                    if sql.startswith("TRUNCATE")
                    else "append-only",
                ):
                    async with sessions() as db:
                        await db.execute(text(sql), {"route_id": route_id})
                        await db.commit()

        await storage.transition(
            route_id, "RECHECKING", "SYNTHETIC manual recheck", NOW
        )
        restarted = OpportunityStore(sessions, uuid4())
        # Restart processing does not need a new run FK until a new evaluation is saved.
        await restarted.expire_previous_runs(NOW + timedelta(seconds=10))
        async with sessions() as db:
            route = await db.get(ArbitrageRoute, route_id)
            assert route.status == "EXPIRED" and route.revision == 2
            events = (
                await db.scalars(
                    select(ArbitrageLifecycleEvent).where(
                        ArbitrageLifecycleEvent.route_id == route_id
                    )
                )
            ).all()
            assert {event.status for event in events} == {
                "DETECTED",
                "RESEARCH_ONLY",
                "RECHECKING",
                "EXPIRED",
            }
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ArbitrageEvaluation)
                    .where(ArbitrageEvaluation.route_id == route_id)
                )
                == 2
            )
    finally:
        await database.dispose()
