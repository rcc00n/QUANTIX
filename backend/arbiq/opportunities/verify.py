"""Read-only actual running scanner evidence. Never seeds financial fixtures."""

import asyncio
import json
from datetime import UTC, datetime
from statistics import median

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from arbiq.core.config import get_settings
from arbiq.db.models import CollectorRun, Exchange, ExchangeHealth, LatestMarketBook, Market
from arbiq.opportunities.coverage import venue_group


async def main():
    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with aiohttp.ClientSession(
            base_url=settings.collector_url,
            headers={
                "Authorization": "Bearer " + settings.collector_internal_token.get_secret_value()
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as client:
            async with client.get("/engine/current") as response:
                response.raise_for_status()
                actual = await response.json()
        diagnostics = actual["diagnostics"]
        clock = diagnostics.get("clock", {})
        now = (
            datetime.fromisoformat(clock["utc_now"]) if clock.get("trusted") else datetime.now(UTC)
        )
        async with async_sessionmaker(engine)() as db:
            run_id = await db.scalar(
                select(CollectorRun.id).where(CollectorRun.status == "running")
            )
            rows = (
                await db.execute(
                    select(Exchange, ExchangeHealth)
                    .join(ExchangeHealth)
                    .where(Exchange.enabled.is_(True))
                )
            ).all()
            books = (
                await db.scalars(
                    select(LatestMarketBook)
                    .join(Market)
                    .where(
                        LatestMarketBook.run_id == run_id,
                        Market.metadata_json["monitored"].as_boolean().is_(True),
                    )
                )
            ).all()
        working = [
            exchange.slug
            for exchange, health in rows
            if health.last_success_at and health.run_id == run_id
        ]
        ages = [(now - book.received_at).total_seconds() for book in books]
        latency = [
            book.payload["latency_ms"]
            for book in books
            if book.payload.get("latency_ms") is not None
        ]
        result = {
            "measured_at": now.isoformat(),
            "kind": "ACTUAL_PUBLIC_API_EVIDENCE",
            "diagnostics": diagnostics,
            "working_adapters": working,
            "connected_venue_groups": len({venue_group(x) for x in working}),
            "discovered_spot_listings": sum(health.market_count for _, health in rows),
            "monitored_markets": sum(health.monitored_count for _, health in rows),
            "persisted_book_count": len(books),
            "persisted_book_ingestion_age_seconds": {
                "min": min(ages) if ages else None,
                "median": median(ages) if ages else None,
                "max": max(ages) if ages else None,
            },
            "persisted_rest_latency_ms": {
                "min": min(latency) if latency else None,
                "median": median(latency) if latency else None,
                "max": max(latency) if latency else None,
            },
            "transport_counts": {
                source: sum(b.payload.get("source") == source for b in books)
                for source in {b.payload.get("source") for b in books}
            },
            "routes": actual["items"],
            "actionable": False,
        }
        print(json.dumps(result, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
