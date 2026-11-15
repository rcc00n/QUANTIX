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


@pytest.mark.parametrize("invalid", ["source", "receipt"])
async def test_latest_fallback_recovers_from_untrusted_future_time(database_url, invalid):
    engine, sessions, store, slug, _exchange_id, market = await seed_store(database_url)
    try:
        book = synthetic_capture(market_id=market.id, exchange=slug)
        fields = {"ingestion_index": 1, "state": book.state.__class__.SUSPICIOUS}
        if invalid == "source":
            fields.update(
                exchange_timestamp=book.received_at + timedelta(seconds=80),
                reasons=("invalid_or_future_timestamp",),
            )
        else:
            fields.update(
                received_at=book.received_at + timedelta(seconds=80),
                verification={"clock": {"trusted": False}},
                reasons=("collector_clock_jump",),
            )
        await store.save_book(book.model_copy(update=fields), force=True)
        await store.save_book(book.model_copy(update={"ingestion_index": 2}), force=True)
        async with sessions() as db:
            latest = await db.get(LatestMarketBook, market.id)
            assert latest.received_at == book.received_at
            assert latest.exchange_timestamp == book.exchange_timestamp
            assert latest.payload["ingestion_index"] == 2
        await store.stop()
    finally:
        await engine.dispose()


async def test_retention_prunes_only_transient_rows_and_enforces_row_budget(
    database_url,
):
    config = CollectorConfig(max_transient_rows=1000)
    engine, sessions, store, _slug, _exchange_id, market = await seed_store(database_url, config)
    now = datetime.now(UTC)
    try:
        async with sessions() as db:
            snapshot = OrderBookSnapshot(
                market_id=market.id,
                observed_at=now,
                received_at=now,
                bids=[["100", "1"]],
                asks=[["101", "1"]],
                source="synthetic-financial-evidence",
                quality="SYNTHETIC",
                content_sha256="a" * 64,
            )
            db.add(snapshot)
            await db.flush()
            snapshot_id = snapshot.id
            db.add(
                MarketQualityEvent(
                    event_key=uuid4().hex,
                    state="SUSPICIOUS",
                    market_id=market.id,
                    created_at=now - timedelta(days=10),
                    reasons=["synthetic_expired"],
                    evidence={"synthetic": True},
                    confirmation={},
                    snapshot_id=snapshot_id,
                )
            )
            values = [
                {
                    "id": uuid4(),
                    "market_id": market.id,
                    "bucket_start": now - timedelta(seconds=i),
                    "received_at": now - timedelta(seconds=i),
                    "exchange_timestamp": now - timedelta(seconds=i),
                    "bid": Decimal(100),
                    "ask": Decimal(101),
                    "state": "HEALTHY",
                    "latency_ms": 1,
                    "content_sha256": "b" * 64,
                    "created_at": now - timedelta(seconds=i),
                }
                for i in range(1003)
            ]
            await db.execute(insert(MarketDataSample).values(values))
            await db.commit()
        await store.prune()
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(MarketDataSample)) <= 1000
            assert await db.get(OrderBookSnapshot, snapshot_id) is not None
            assert not await db.scalar(
                select(MarketQualityEvent.id).where(MarketQualityEvent.snapshot_id == snapshot_id)
            )
    finally:
        await store.stop()
        await engine.dispose()


def test_market_dashboard_api_authentication_catalog_and_stale_fallback(database_url):
    async def seed():
        engine, _sessions, store, slug, _exchange_id, market = await seed_store(database_url)
        try:
            book = synthetic_capture(
                market_id=market.id,
                exchange=slug,
                received_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
            await store.save_book(book, force=True)
            async with _sessions() as db:
                db.add(
                    CollectorRun(
                        worker_id=f"synthetic-future-stopped-{uuid4()}",
                        status="stopped",
                        created_at=datetime(2099, 1, 1, tzinfo=UTC),
                        heartbeat_at=datetime(2020, 1, 1, tzinfo=UTC),
                        config_snapshot={},
                        metrics={},
                    )
                )
                await db.commit()
            return slug, str(market.id)
        finally:
            await engine.dispose()

    slug, market_id = asyncio.run(seed())
    username = f"synthetic-api-{uuid4().hex}"
    config = Settings(
        _env_file=None,
        database_url=database_url,
        admin_username=username,
        admin_password_hash=hash_password("synthetic-api-password"),
        collector_internal_token="",
    )
    with TestClient(create_app(config), client=(f"synthetic-{uuid4().hex}", 12345)) as client:
        assert client.get(f"/api/v1/markets/{market_id}/book").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": "synthetic-api-password"},
            ).status_code
            == 200
        )
        feeds = client.get("/api/v1/exchanges").json()
        assert any(feed["slug"] == slug for feed in feeds)
        catalog = client.get(f"/api/v1/exchanges/{slug}/markets?monitored_only=true").json()
        assert catalog["total"] == 1 and catalog["markets"][0]["symbol"] == "BTC/USD"
        response = client.get(f"/api/v1/markets/{market_id}/book")
        assert response.status_code == 200
        book = response.json()
        assert book["state"] == "STALE" and book["storage_origin"] == "persisted_cache"
        assert book["actionable"] is False and book["exchange_timestamp"]
        assert client.get(f"/api/v1/exchanges/{slug}/quality").status_code == 200
        assert (
            client.get("/api/v1/market-data/status").json()["arbitrage_engine"] == "depth_scanner"
        )
        assert client.post(f"/api/v1/markets/{market_id}/confirm").status_code == 503
        assert client.get("/api/v1/exchanges/synthetic-missing/markets").status_code == 404


def test_operator_rename_and_rotation_preserve_history_and_revoke_sessions(
    database_url,
):
    suffix = uuid4().hex
    old_name = f"synthetic-old-{suffix}"
    new_name = f"synthetic-new-{suffix}"
    old_token = f"synthetic-old-session-{suffix}"

    async def seed():
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                user = User(
                    username=old_name,
                    password_hash=hash_password("synthetic-old-password"),
                )
                db.add(user)
                await db.flush()
                strategy = StrategyVersion(
                    user_id=user.id,
                    version=1,
                    settings={"synthetic": True},
                    reason="SYNTHETIC fixture",
                )
                session = AuthSession(
                    user_id=user.id,
                    token_hash=hash_token(old_token),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                db.add_all([strategy, session])
                await db.commit()
                return user.id, strategy.id, session.id
        finally:
            await engine.dispose()

    user_id, strategy_id, session_id = asyncio.run(seed())
    config = Settings(
        _env_file=None,
        database_url=database_url,
        admin_username=new_name,
        admin_previous_username=old_name,
        admin_password_hash=hash_password("synthetic-new-password"),
    )
    with TestClient(create_app(config), client=(f"synthetic-rotation-{suffix}", 12345)) as client:
        client.cookies.set("arbiq_session", old_token)
        assert client.get("/api/v1/auth/me").status_code == 401
        client.cookies.clear()
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": old_name, "password": "synthetic-old-password"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": new_name, "password": "synthetic-new-password"},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/auth/me").json()["id"] == str(user_id)

    async def verify():
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                assert (await db.get(StrategyVersion, strategy_id)).user_id == user_id
                assert (await db.get(AuthSession, session_id)).revoked_at is not None
                assert (await db.get(User, user_id)).username == new_name
        finally:
            await engine.dispose()

    asyncio.run(verify())
