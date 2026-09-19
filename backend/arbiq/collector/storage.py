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

