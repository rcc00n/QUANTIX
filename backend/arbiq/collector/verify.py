"""Read-only live verification report from the actual running collector's records."""

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from statistics import median

import aiohttp
import ccxt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from arbiq.core.config import get_settings
from arbiq.db.models import CollectorRun, Exchange, ExchangeHealth, LatestMarketBook, Market
from arbiq.market_data.quality import CapturedBook, fresh_view


async def report():
    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    runtime = {}
    try:
        async with aiohttp.ClientSession(
            headers={
                "Authorization": "Bearer " + settings.collector_internal_token.get_secret_value()
            },
            timeout=aiohttp.ClientTimeout(total=5),
        ) as client:
            async with client.get(settings.collector_url + "/engine/diagnostics") as response:
                response.raise_for_status()
                runtime = await response.json()
        clock = runtime.get("clock", {})
        if clock.get("trusted"):
            now = datetime.fromisoformat(clock["utc_now"])
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        runtime = {}
    try:
        async with sessions() as db:
            run = await db.scalar(
                select(CollectorRun).where(CollectorRun.status == "running").limit(1)
            )
            if run is None:
                run = await db.scalar(
                    select(CollectorRun).order_by(CollectorRun.created_at.desc()).limit(1)
                )
            records = (
                await db.execute(
                    select(Exchange, ExchangeHealth)
                    .outerjoin(ExchangeHealth)
                    .order_by(Exchange.slug)
                )
            ).all()
            books = (
                await db.execute(select(LatestMarketBook, Market.exchange_id).join(Market))
            ).all()
            by_exchange = {}
            for row, exchange_id in books:
                if run and row.run_id == run.id:
                    book = fresh_view(
                        CapturedBook.model_validate(row.payload),
                        now,
                        settings.collector.maximum_age_seconds,
                    )
                    by_exchange.setdefault(exchange_id, []).append(book)
            rows = []
            for exchange, health in records:
                current = bool(run and health and health.run_id == run.id)
                data = by_exchange.get(exchange.id, [])
                structurally_available = [
                    book
                    for book in data
                    if book.normalized.bids
                    and book.normalized.asks
                    and not any(
                        reason.startswith("invalid_")
                        or reason.startswith("unordered_")
                        or reason == "symbol_mismatch"
                        for reason in book.reasons
                    )
                ]
                ingested_ages = [
                    round((now - book.received_at).total_seconds(), 3) for book in data
                ]
                exemplar = next(iter(structurally_available), None)
                rows.append(
                    {
                        "exchange": exchange.slug,
                        "name": exchange.name,
                        "configured": exchange.enabled,
                        "venue_group": "coinbase"
                        if exchange.slug == "coinbaseexchange"
                        else exchange.slug,
                        "live_verified": bool(
                            current and health.last_success_at and structurally_available
                        ),
                        "operational_state": health.state if current else "UNAVAILABLE",
                        "market_count": health.market_count if health else 0,
                        "monitored_markets": health.monitored_count if current else 0,
                        "latest_books": len(data),
                        "structurally_available_books": len(structurally_available),
                        "cad_markets": health.cad_market_count if health else 0,
                        "mode": health.connection_mode if current else "not_connected",
                        "last_success_at": health.last_success_at.isoformat()
                        if health and health.last_success_at
                        else None,
                        "last_rest_fetch_ms": health.latency_ms if current else None,
                        "ingestion_age_seconds": {
                            "minimum": min(ingested_ages),
                            "median": median(ingested_ages),
                            "maximum": max(ingested_ages),
                        }
                        if ingested_ages
                        else None,
                        "quality_counts": dict(Counter(book.state.value for book in data)),
                        "quality_reasons": dict(
                            Counter(reason for book in data for reason in book.reasons)
                        ),
                        "book_sources": dict(Counter(book.source for book in data)),
                        "successful_updates": health.successful_requests if current else 0,
                        "failed_requests": health.failed_requests if current else 0,
                        "reconnects": health.reconnects if current else 0,
                        "error_code": health.error_code
                        if health
                        else exchange.capabilities.get("error"),
                        "sample": {
                            "symbol": exemplar.symbol,
                            "received_at": exemplar.received_at.isoformat(),
                            "exchange_timestamp": exemplar.exchange_timestamp.isoformat()
                            if exemplar.exchange_timestamp
                            else None,
                            "bid": str(exemplar.normalized.bids[0].price),
                            "ask": str(exemplar.normalized.asks[0].price),
                            "source": exemplar.source,
                            "content_sha256": exemplar.content_sha256,
                        }
                        if exemplar
                        else None,
                        "canadian_eligibility": exchange.capabilities.get("canadian_eligibility"),
                        "manual_trading_eligible": False,
                    }
                )
            verified = [row for row in rows if row["configured"] and row["live_verified"]]
            return {
                "product": "QUANTIX",
                "checked_at": now.isoformat(),
                "ccxt_version": ccxt.__version__,
                "verification_kind": "actual_public_api_records_from_running_collector",
                "collector_status": run.status if run else None,
                "heartbeat_at": run.heartbeat_at.isoformat() if run else None,
                "run_id": str(run.id) if run else None,
                "configured_adapters": sum(row["configured"] for row in rows),
                "successfully_verified_adapters": len(verified),
                "independent_venue_groups": len({row["venue_group"] for row in verified}),
                "discovered_spot_listings": sum(
                    row["market_count"] for row in rows if row["configured"]
                ),
                "monitored_markets": sum(
                    row["monitored_markets"] for row in rows if row["configured"]
                ),
                "latest_books": sum(row["latest_books"] for row in rows if row["configured"]),
                "exchanges": rows,
                "arbitrage_engine": {
                    "health_available": bool(runtime),
                    "healthy": runtime.get("healthy"),
                    "metrics": runtime.get("metrics", {}),
                },
                "clock": runtime.get("clock", {"trusted": False, "reason": "RUNTIME_UNAVAILABLE"}),
                "mode": "paper",
                "notes": [
                    "Snapshot of actual records; statuses and freshness vary over time.",
                    "Adapter counts overlap; both Coinbase adapters share one venue group.",
                    "REST timing includes adapter throttling/parsing; excludes scheduling.",
                    "WebSocket has no REST timing. Missing timestamps remain explicit.",
                    "Public access does not approve Canadian trading or establish profit.",
                    "Trusted collector UTC is used; host fallback is untrusted.",
                ],
            }
    finally:
        await engine.dispose()


def main():
    print(json.dumps(asyncio.run(report()), indent=2))


if __name__ == "__main__":
    main()
