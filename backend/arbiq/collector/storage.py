"""Transactional persistence. Only explicitly transient Stage 2 tables have retention."""

import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import BigInteger, and_, case, delete, false, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from arbiq.collector.config import CollectorConfig
from arbiq.db.models import (
    Asset,
    CollectorRun,
    Exchange,
    ExchangeConfigVersion,
    ExchangeHealth,
    LatestMarketBook,
    Market,
    MarketDataSample,
    MarketQualityEvent,
    OrderBookSnapshot,
    SystemEvent,
)
from arbiq.market_data.discovery import DiscoveredMarket
from arbiq.market_data.quality import CapturedBook, DataState, safe_json


class CollectorStore:
    def __init__(self, sessions, config: CollectorConfig):
        self.sessions = sessions
        self.config = config
        self.run_id = None
        self.last_persist: dict = {}
        self.last_quality: dict = {}
        self.last_events: dict = {}
        self.last_sample: dict = {}

    async def start(self, worker_id: str):
        now = datetime.now(UTC)
        async with self.sessions() as db:
            await db.execute(
                update(CollectorRun)
                .where(CollectorRun.status == "running")
                .values(status="abandoned", stopped_at=now)
            )
            run = CollectorRun(
                worker_id=worker_id,
                status="running",
                heartbeat_at=now,
                config_snapshot=self.config.model_dump(mode="json"),
                metrics={},
            )
            db.add(run)
            await db.flush()
            self.run_id = run.id
            db.add(
                SystemEvent(
                    event_type="collector.started",
                    severity="info",
                    message="Public market-data collector started",
                    details={"run_id": str(run.id)},
                )
            )
            await db.commit()
        return self.run_id

    async def stop(self):
        if not self.run_id:
            return
        async with self.sessions() as db:
            await db.execute(
                update(CollectorRun)
                .where(CollectorRun.id == self.run_id)
                .values(
                    status="stopped", stopped_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC)
                )
            )
            db.add(
                SystemEvent(
                    event_type="collector.stopped",
                    severity="info",
                    message="Public market-data collector stopped",
                    details={"run_id": str(self.run_id)},
                )
            )
            await db.commit()

    async def disable_unconfigured(self, configured: set[str]):
        async with self.sessions() as db:
            rows = (
                await db.scalars(
                    select(Exchange).where(
                        Exchange.enabled.is_(True), Exchange.slug.not_in(configured)
                    )
                )
            ).all()
            for exchange in rows:
                exchange.enabled = False
                exchange.config_version += 1
                db.add(
                    ExchangeConfigVersion(
                        exchange_id=exchange.id,
                        version=exchange.config_version,
                        effective_at=datetime.now(UTC),
                        enabled=False,
                        capabilities=exchange.capabilities,
                        public_settings=self.config.model_dump(mode="json"),
                        reason="Removed from collector configuration",
                    )
                )
            await db.commit()

    async def register_exchange(self, slug: str, name: str, capabilities: dict):
        async with self.sessions() as db:
            exchange = await db.scalar(select(Exchange).where(Exchange.slug == slug))
            stable = {k: v for k, v in capabilities.items() if k != "discovered_at"}
            if exchange is None:
                exchange = Exchange(
                    slug=slug, name=name, enabled=True, capabilities=stable, config_version=1
                )
                db.add(exchange)
                await db.flush()
                changed = True
            else:
                changed = exchange.capabilities != stable
                exchange.name = name
                exchange.enabled = True
                if changed:
                    exchange.config_version += 1
                    exchange.capabilities = stable
            if changed:
                db.add(
                    ExchangeConfigVersion(
                        exchange_id=exchange.id,
                        version=exchange.config_version,
                        effective_at=datetime.now(UTC),
                        enabled=True,
                        capabilities=stable,
                        public_settings=self.config.model_dump(mode="json"),
                        reason="Public adapter capability discovery",
                    )
                )
            await db.commit()
            return exchange.id

    async def save_markets(
        self, exchange_id, markets: list[DiscoveredMarket], selected: list[DiscoveredMarket]
    ):
        assets = {}
        for market in markets:
            for asset in (market.base, market.quote):
                assets[asset.key] = {
                    "id": uuid4(),
                    "canonical_key": asset.key,
                    "symbol": asset.symbol,
                    "kind": asset.kind,
                    "network": None,
                    "identifiers": {"identity_verified": asset.verified, "reason": asset.reason},
                }
        async with self.sessions() as db:
            values = list(assets.values())
            for offset in range(0, len(values), 500):
                await db.execute(
                    insert(Asset)
                    .values(values[offset : offset + 500])
                    .on_conflict_do_nothing(index_elements=[Asset.canonical_key])
                )
            asset_ids = dict(
                (
                    await db.execute(
                        select(Asset.canonical_key, Asset.id).where(Asset.canonical_key.in_(assets))
                    )
                ).all()
            )
            existing = {
                row.native_symbol: row
                for row in (
                    await db.execute(
                        select(
                            Market.native_symbol, Market.base_asset_id, Market.quote_asset_id
                        ).where(Market.exchange_id == exchange_id)
                    )
                ).all()
            }
            # Disappearance/inactivation does not delete historical market identity.
            await db.execute(
                update(Market)
                .where(Market.exchange_id == exchange_id)
                .values(
                    active=False,
                    metadata_json=Market.metadata_json.concat(
                        {"monitored": False, "active_status": "not_rediscovered"}
                    ),
                )
            )
            chosen = {market.native_symbol for market in selected}
            values = []
            conflicts = []
            for market in markets:
                base_id, quote_id = asset_ids[market.base.key], asset_ids[market.quote.key]
                prior = existing.get(market.native_symbol)
                if prior and (prior.base_asset_id, prior.quote_asset_id) != (base_id, quote_id):
                    conflicts.append(market.native_symbol)
                    continue
                values.append(
                    {
                        "id": uuid4(),
                        "exchange_id": exchange_id,
                        "base_asset_id": base_id,
                        "quote_asset_id": quote_id,
                        "native_symbol": market.native_symbol,
                        "active": market.active is not False,
                        "amount_step": market.amount_step,
                        "price_tick": market.price_tick,
                        "min_notional": market.min_notional,
                        "metadata_json": {
                            **market.metadata,
                            "monitored": market.native_symbol in chosen,
                            "discovered_at": datetime.now(UTC).isoformat(),
                        },
                    }
                )
            for offset in range(0, len(values), 300):
                statement = insert(Market).values(values[offset : offset + 300])
                await db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[Market.exchange_id, Market.native_symbol],
                        set_={
                            key: getattr(statement.excluded, key)
                            for key in (
                                "active",
                                "amount_step",
                                "price_tick",
                                "min_notional",
                                "metadata_json",
                            )
                        },
                    )
                )
            if conflicts:
                db.add(
                    SystemEvent(
                        exchange_id=exchange_id,
                        event_type="market.identity_conflict",
                        severity="error",
                        message="Conflicting market identities quarantined",
                        details={"symbols": conflicts[:50], "count": len(conflicts)},
                    )
                )
            mapping = dict(
                (
                    await db.execute(
                        select(Market.native_symbol, Market.id).where(
                            Market.exchange_id == exchange_id
                        )
                    )
                ).all()
            )
            await db.commit()
        return [
            replace(market, id=mapping[market.native_symbol])
            for market in selected
            if market.native_symbol not in conflicts and market.native_symbol in mapping
        ]

    async def set_selection(self, exchange_id, selected):
        requested = {market.native_symbol for market in selected}
        async with self.sessions() as db:
            mapping = {}
            if requested:
                rows = await db.execute(
                    select(Market.native_symbol, Market.id).where(
                        Market.exchange_id == exchange_id,
                        Market.active.is_(True),
                        Market.native_symbol.in_(requested),
                    )
                )
                mapping = dict(rows.all())
            should_monitor = Market.native_symbol.in_(mapping) if mapping else false()
            monitored = Market.metadata_json["monitored"].as_boolean()
            await db.execute(
                update(Market)
                .where(
                    Market.exchange_id == exchange_id, monitored.is_distinct_from(should_monitor)
                )
                .values(
                    metadata_json=case(
                        (should_monitor, Market.metadata_json.concat({"monitored": True})),
                        else_=Market.metadata_json.concat({"monitored": False}),
                    )
                )
            )
            await db.commit()
        return [
            replace(m, id=mapping[m.native_symbol]) for m in selected if m.native_symbol in mapping
        ]

    async def health(self, exchange_id, values: dict):
        async with self.sessions() as db:
            statement = insert(ExchangeHealth).values(
                exchange_id=exchange_id, run_id=self.run_id, **values
            )
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[ExchangeHealth.exchange_id],
                    set_={key: getattr(statement.excluded, key) for key in (*values, "run_id")},
                )
            )
            await db.commit()

    async def heartbeat(self, metrics: dict):
        async with self.sessions() as db:
            await db.execute(
                update(CollectorRun)
                .where(CollectorRun.id == self.run_id)
                .values(heartbeat_at=datetime.now(UTC), metrics=safe_json(metrics))
            )
            await db.commit()

    async def event(
        self, exchange_id, event_type: str, message: str, severity="info", details=None
    ):
        now = time.monotonic()
        key = (exchange_id, event_type, json.dumps(details or {}, sort_keys=True))
        if now - self.last_events.get(key, -1000) < 60:
            return
        # Key space is bounded even when adapters rotate error variants.
        if len(self.last_events) > 1000:
            self.last_events.clear()
        self.last_events[key] = now
        async with self.sessions() as db:
            db.add(
                SystemEvent(
                    exchange_id=exchange_id,
                    event_type=event_type,
                    severity=severity,
                    message=message,
                    details=safe_json(details or {}),
                )
            )
            await db.commit()

    async def save_book(self, book: CapturedBook, force=False):
        now = time.monotonic()
        signature = (book.state, book.reasons)
        transition = self.last_quality.get(book.market_id) != signature
        if (
            not force
            and not transition
            and now - self.last_persist.get(book.market_id, -1000)
            < self.config.persistence_interval_seconds
        ):
            return False
        payload = book.model_dump(mode="json")
        invalid_prior_time = or_(
            LatestMarketBook.payload["verification"]["clock"]["trusted"].as_boolean().is_(False),
            LatestMarketBook.payload["reasons"].contains(["invalid_or_future_timestamp"]),
            LatestMarketBook.payload["reasons"].contains(["collector_clock_jump"]),
            LatestMarketBook.payload["reasons"].contains(["ingestion_clock_reversed"]),
            LatestMarketBook.exchange_timestamp > LatestMarketBook.received_at,
        )
        async with self.sessions() as db:
            statement = insert(LatestMarketBook).values(
                market_id=book.market_id,
                run_id=self.run_id,
                received_at=book.received_at,
                exchange_timestamp=book.exchange_timestamp,
                state=book.state.value,
                payload=payload,
            )
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[LatestMarketBook.market_id],
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in (
                            "run_id",
                            "received_at",
                            "exchange_timestamp",
                            "state",
                            "payload",
                        )
                    },
                    where=and_(
                        or_(
                            invalid_prior_time,
                            LatestMarketBook.received_at <= statement.excluded.received_at,
                        ),
                        or_(
                            invalid_prior_time,
                            LatestMarketBook.exchange_timestamp.is_(None),
                            statement.excluded.exchange_timestamp.is_(None),
                            LatestMarketBook.exchange_timestamp
                            <= statement.excluded.exchange_timestamp,
                        ),
                        or_(
                            LatestMarketBook.run_id != statement.excluded.run_id,
                            func.coalesce(
                                LatestMarketBook.payload["ingestion_index"].astext.cast(BigInteger),
                                0,
                            )
                            <= book.ingestion_index,
                        ),
                    ),
                )
            )
            if (
                now - self.last_sample.get(book.market_id, -10000)
                >= self.config.sample_interval_seconds
            ):
                seconds = int(book.received_at.timestamp())
                bucket = datetime.fromtimestamp(
                    seconds - seconds % self.config.sample_interval_seconds, UTC
                )
                statement = insert(MarketDataSample).values(
                    id=uuid4(),
                    market_id=book.market_id,
                    bucket_start=bucket,
                    received_at=book.received_at,
                    exchange_timestamp=book.exchange_timestamp,
                    bid=book.normalized.bids[0].price if book.normalized.bids else None,
                    ask=book.normalized.asks[0].price if book.normalized.asks else None,
                    state=book.state.value,
                    latency_ms=book.latency_ms,
                    content_sha256=book.content_sha256,
                )
                await db.execute(
                    statement.on_conflict_do_nothing(index_elements=["market_id", "bucket_start"])
                )
                self.last_sample[book.market_id] = now
            if transition or force:
                snapshot_id = None
                if book.state == DataState.SUSPICIOUS and self.config.evidence_budget_per_hour:
                    cutoff = datetime.now(UTC) - timedelta(hours=1)
                    count = await db.scalar(
                        select(func.count())
                        .select_from(OrderBookSnapshot)
                        .where(
                            OrderBookSnapshot.created_at >= cutoff,
                            OrderBookSnapshot.source.like("collector:%"),
                        )
                    )
                    prior = await db.scalar(
                        select(OrderBookSnapshot.id)
                        .where(
                            OrderBookSnapshot.market_id == book.market_id,
                            OrderBookSnapshot.content_sha256 == book.content_sha256,
                        )
                        .limit(1)
                    )
                    recent = await db.scalar(
                        select(OrderBookSnapshot.id)
                        .where(
                            OrderBookSnapshot.market_id == book.market_id,
                            OrderBookSnapshot.created_at >= cutoff,
                            OrderBookSnapshot.source.like("collector:%"),
                        )
                        .limit(1)
                    )
                    if prior:
                        snapshot_id = prior
                    elif count < self.config.evidence_budget_per_hour and not recent:
                        snapshot = OrderBookSnapshot(
                            market_id=book.market_id,
                            observed_at=book.exchange_timestamp or book.received_at,
                            received_at=book.received_at,
                            bids=[[str(v.price), str(v.quantity)] for v in book.normalized.bids],
                            asks=[[str(v.price), str(v.quantity)] for v in book.normalized.asks],
                            sequence=book.sequence,
                            source=f"collector:{book.source}",
                            quality=book.state.value,
                            content_sha256=book.content_sha256,
                        )
                        db.add(snapshot)
                        await db.flush()
                        snapshot_id = snapshot.id
                bucket = int(book.received_at.timestamp()) // 60
                key = hashlib.sha256(
                    f"{book.market_id}:{bucket}:{book.state}:{book.reasons}".encode()
                ).hexdigest()
                await db.execute(
                    insert(MarketQualityEvent)
                    .values(
                        id=uuid4(),
                        event_key=key,
                        market_id=book.market_id,
                        state=book.state.value,
                        reasons=list(book.reasons),
                        evidence={
                            "raw": book.raw_evidence,
                            "content_sha256": book.content_sha256,
                            "received_at": book.received_at.isoformat(),
                            "source": book.source,
                        },
                        confirmation=book.verification,
                        snapshot_id=snapshot_id,
                    )
                    .on_conflict_do_nothing(index_elements=[MarketQualityEvent.event_key])
                )
            await db.commit()
        self.last_persist[book.market_id] = now
        self.last_quality[book.market_id] = signature
        return True

    async def latest_watermarks(self, market_ids):
        """Ordering only: retained fallback is never restored as fresh live data."""
        if not market_ids:
            return {}
        if len(market_ids) > self.config.max_markets_per_exchange:
            raise ValueError("Bounded venue watermark read required")
        async with self.sessions() as db:
            rows = (
                await db.execute(
                    select(
                        LatestMarketBook.market_id,
                        LatestMarketBook.received_at,
                        LatestMarketBook.exchange_timestamp,
                    ).where(
                        LatestMarketBook.market_id.in_(market_ids),
                        LatestMarketBook.state.in_(["HEALTHY", "DEGRADED"]),
                        LatestMarketBook.payload["verification"]["clock"]["trusted"]
                        .as_boolean()
                        .is_(True),
                    )
                )
            ).all()
        return {row.market_id: (row.received_at, row.exchange_timestamp) for row in rows}

