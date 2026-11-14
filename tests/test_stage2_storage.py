"""PostgreSQL/API integration. Every venue, price and credential here is SYNTHETIC."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from arbiq.auth.security import hash_password, hash_token
from arbiq.collector.config import CollectorConfig
from arbiq.collector.storage import CollectorStore
from arbiq.core.config import Settings
from arbiq.db.models import (
    AuthSession,
    CollectorRun,
    LatestMarketBook,
    Market,
    MarketDataSample,
    MarketQualityEvent,
    OrderBookSnapshot,
    StrategyVersion,
    SystemEvent,
    User,
)
from arbiq.main import create_app
from arbiq.market_data.discovery import normalize_market
from fastapi.testclient import TestClient
from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_market_data import synthetic_capture, synthetic_market


async def seed_store(database_url, config=None):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    config = config or CollectorConfig()
    store = CollectorStore(sessions, config)
    await store.start(f"synthetic-worker-{uuid4()}")
    slug = f"synthetic-{uuid4().hex}"
    exchange_id = await store.register_exchange(
        slug,
        "SYNTHETIC fixture venue",
        {
            "public_market_data_supported": True,
            "canadian_eligibility": {"classification": "UNVERIFIED"},
        },
    )
    market = normalize_market(slug, synthetic_market(), {}, precision_mode=4)
    selected = await store.save_markets(exchange_id, [market], [market])
    return engine, sessions, store, slug, exchange_id, selected[0]


async def test_selection_only_rewrites_rows_with_a_changed_monitor_flag(database_url):
    engine, sessions, store, slug, exchange_id, original = await seed_store(database_url)
    try:
        replacement = normalize_market(slug, synthetic_market(base="ETH"), {}, precision_mode=4)
        unselected = normalize_market(slug, synthetic_market(base="SOL"), {}, precision_mode=4)
        await store.save_markets(exchange_id, [original, replacement, unselected], [original])

        async def inspect_selection():
            async with sessions() as db:
                rows = await db.execute(
                    select(
                        Market.native_symbol, Market.metadata_json, literal_column("ctid::text")
                    ).where(Market.exchange_id == exchange_id)
                )
                return {symbol: (metadata, row_version) for symbol, metadata, row_version in rows}

        before = await inspect_selection()
        assert [m.native_symbol for m in await store.set_selection(exchange_id, [replacement])] == [
            replacement.native_symbol
        ]
        after = await inspect_selection()
        assert after[unselected.native_symbol] == before[unselected.native_symbol]
        assert after[original.native_symbol][0] == {
            **before[original.native_symbol][0],
            "monitored": False,
        }
        assert after[replacement.native_symbol][0] == {
            **before[replacement.native_symbol][0],
            "monitored": True,
        }
        await store.set_selection(exchange_id, [replacement])
        assert await inspect_selection() == after
        assert await store.set_selection(exchange_id, []) == []
        assert all(
            not metadata["monitored"] for metadata, _ in (await inspect_selection()).values()
        )
        await store.stop()
    finally:
        await engine.dispose()


async def test_store_discovery_upserts_restart_idempotency_and_evidence(database_url):
    engine, sessions, store, slug, exchange_id, market = await seed_store(database_url)
    try:
        again = await store.save_markets(exchange_id, [market], [market])
        assert again[0].id == market.id
        book = synthetic_capture(market_id=market.id, exchange=slug)
        await store.save_book(book, force=True)
        await store.save_book(book, force=True)
        restarted = CollectorStore(sessions, CollectorConfig())
        await restarted.start(f"synthetic-restart-{uuid4()}")
        await restarted.save_book(book, force=True)
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Market)
                    .where(Market.exchange_id == exchange_id)
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(LatestMarketBook)
                    .where(LatestMarketBook.market_id == market.id)
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(MarketDataSample)
                    .where(MarketDataSample.market_id == market.id)
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(MarketQualityEvent)
                    .where(MarketQualityEvent.market_id == market.id)
                )
                == 1
            )
            event = await db.scalar(
                select(MarketQualityEvent).where(MarketQualityEvent.market_id == market.id)
            )
            assert event.evidence["raw"]["bids"][0][0] == "100"
        causal = book.model_copy(update={"ingestion_index": 1})
        await restarted.save_book(causal, force=True)
        after_clock_step = book.model_copy(
            update={
                "ingestion_index": 2,
                "received_at": book.received_at - timedelta(seconds=90),
                "state": book.state.__class__.SUSPICIOUS,
                "reasons": ("collector_clock_jump",),
            }
        )
        await restarted.save_book(after_clock_step, force=True)
        async with sessions() as db:
            latest = await db.get(LatestMarketBook, market.id)
            assert latest.payload["ingestion_index"] == 1
        # Changed identity behind the same symbol must not rewrite historical FKs.
        conflict = normalize_market(
            slug,
            synthetic_market(),
            {"BTC": {"name": "Wrapped Bitcoin"}},
            precision_mode=4,
        )
        assert not await restarted.save_markets(exchange_id, [conflict], [conflict])
        async with sessions() as db:
            events = (
                await db.scalars(select(SystemEvent).where(SystemEvent.exchange_id == exchange_id))
            ).all()
            assert any(e.event_type == "market.identity_conflict" for e in events)
        await restarted.stop()
    finally:
        await engine.dispose()








