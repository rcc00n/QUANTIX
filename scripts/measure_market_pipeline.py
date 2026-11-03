"""Read-only collector measurement; run via stdin inside the API container.

No exchange requests, financial writes, credentials or full books in output.
UTC comes from the collector reference; elapsed time uses CLOCK_MONOTONIC_RAW.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import aiohttp
from arbiq.core.clock import elapsed_seconds
from arbiq.core.config import get_settings
from arbiq.db.models import Exchange, ExchangeHealth, Market
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_size=1, max_overflow=0
    )
    headers = {"Authorization": "Bearer " + settings.collector_internal_token.get_secret_value()}
    seconds = int(os.environ.get("MEASURE_SECONDS", "900"))
    if not 10 <= seconds <= 3600:
        raise ValueError("Measurement duration must be 10..3600 seconds")
    gate = asyncio.Semaphore(4)
    async with aiohttp.ClientSession(
        base_url=settings.collector_url,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
        connector=aiohttp.TCPConnector(limit=4),
    ) as client:

        async def get(path):
            async with gate:
                start = elapsed_seconds()
                try:
                    async with client.get(path) as response:
                        value = await response.json()
                        return {
                            "http": response.status,
                            "read_ms": (elapsed_seconds() - start) * 1000,
                            "value": value,
                        }
                except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                    return {"error": type(exc).__name__}

        start = elapsed_seconds()
        tick = 0
        while True:
            runtime = await get("/terminal/runtime")
            clock = await get("/clock")
            anchor_elapsed = elapsed_seconds()
            reference = clock.get("value", {})
            anchor_utc = (
                datetime.fromisoformat(reference["utc_now"])
                if reference.get("trusted") and reference.get("utc_now")
                else None
            )
            record = {
                "elapsed_seconds": elapsed_seconds() - start,
                "runtime": runtime,
                "clock": clock,
            }
            if tick % 3 == 0:
                scanner = runtime.get("value", {}).get("scanner") or {}
                if scanner.get("metrics", {}).get("pipeline_timings") is not None:
                    feeds = await get("/feeds/status")
                    record["scheduler_timing_windows"] = {
                        slug: metrics["timings"]["scheduling_lag_ms"]
                        for slug, metrics in feeds.get("value", {}).get("items", {}).items()
                        if "scheduling_lag_ms" in metrics.get("timings", {})
                    }
                async with engine.connect() as db:
                    markets = (
                        await db.execute(
                            select(Market.id, Exchange.slug, Market.native_symbol)
                            .join(Exchange, Exchange.id == Market.exchange_id)
                            .where(Market.metadata_json["monitored"].as_boolean().is_(True))
                        )
                    ).all()
                    health = (
                        (
                            await db.execute(
                                select(Exchange.slug, ExchangeHealth.__table__).join(
                                    ExchangeHealth, Exchange.id == ExchangeHealth.exchange_id
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                record["health"] = [dict(row) for row in health]

                async def book(
                    row,
                    anchor_elapsed=anchor_elapsed,
                    anchor_utc=anchor_utc,
                    reference=reference,
                    clock=clock,
                ):
                    result = await get("/books/" + str(row.id))
                    delta = elapsed_seconds() - anchor_elapsed
                    projection_live = (
                        float(reference.get("reference_age_seconds", 90)) + delta <= 90
                    )
                    measured = (
                        anchor_utc + timedelta(seconds=delta)
                        if anchor_utc and projection_live
                        else None
                    )
                    value = result.pop("value", {})
                    return {
                        "measured_at_utc": measured.isoformat() if measured else None,
                        "measurement_uncertainty_ms": float(reference.get("uncertainty_ms") or 2000)
                        + clock.get("read_ms", 0)
                        + result.get("read_ms", 0)
                        + delta * 0.1
                        if measured
                        else None,
                        "measurement_time_basis": (
                            "collector_trusted_utc_projected_by_observer_monotonic_raw; "
                            "uncertainty includes IPC RTT and 100ppm drift"
                        ),
                        "exchange": row.slug,
                        "symbol": row.native_symbol,
                        **result,
                        **{
                            key: value.get(key)
                            for key in (
                                "received_at",
                                "request_started_at",
                                "exchange_timestamp",
                                "latency_ms",
                                "source",
                                "state",
                                "reasons",
                                "ingestion_index",
                                "pipeline",
                            )
                        },
                        "timestamp_basis": value.get("raw_evidence", {}).get("timestamp_basis"),
                        "pipeline": value.get("verification", {}).get("pipeline"),
                    }

                record["books"] = await asyncio.gather(*(book(row) for row in markets))
            print(json.dumps(record, default=str, separators=(",", ":")), flush=True)
            tick += 1
            if elapsed_seconds() - start >= seconds:
                break
            await asyncio.sleep(min(5, max(0, seconds - (elapsed_seconds() - start))))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
