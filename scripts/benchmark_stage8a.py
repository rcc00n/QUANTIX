"""Explicit SYNTHETIC, offline pipeline replay; no DB, exchange or Telegram access.

Uses the same monitored symbol/venue set as a saved production timing sample,
but every book price, quantity, timestamp and fee is a synthetic test fixture.
Run identical script/image limits before and after, in separate processes.
"""

import asyncio
import hashlib
import json
import os
import resource
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid4, uuid5

import arbiq.opportunities.engine as engine_module
from arbiq.core.clock import elapsed_seconds
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.config import EngineConfig
from arbiq.opportunities.engine import ArbitrageEngine
from test_arbitrage_engine import SyntheticCache, capture, market


async def main():
    spec = json.loads(await asyncio.to_thread(Path("/tmp/market-set.json").read_text))
    start = elapsed_seconds()
    cpu_start = time.process_time()

    def now():
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=elapsed_seconds() - start)

    runtimes, books = {}, []
    for i, row in enumerate(spec):
        base, quote = row["symbol"].split("/")
        discovered = replace(
            market(row["exchange"], base, quote),
            id=uuid5(NAMESPACE_URL, f"synthetic-stage8a:{row['exchange']}:{row['symbol']}"),
        )
        rt = runtimes.setdefault(
            row["exchange"],
            SimpleNamespace(
                slug=row["exchange"],
                markets=[],
                catalog=[],
                exchange_id=uuid5(NAMESPACE_URL, "synthetic-stage8a:" + row["exchange"]),
                health={"error_code": None},
            ),
        )
        rt.markets.append(discovered)
        rt.catalog = rt.markets
        books.append(
            capture(
                row["exchange"],
                discovered,
                received_at=now(),
                bid=str(Decimal("100") + Decimal(i % 17) / 100),
                ask=str(Decimal("101") + Decimal(i % 17) / 100),
            )
        )
    collector = SimpleNamespace(
        settings=SimpleNamespace(arbitrage=EngineConfig(evaluation_interval_seconds=0.5)),
        store=SimpleNamespace(sessions=None, run_id=uuid4()),
        runtimes=runtimes,
        cache=SyntheticCache(books),
        request_confirmation=lambda _: "COOLDOWN",
        utc_clock=SimpleNamespace(
            snapshot=lambda: {"trusted": True, "host_healthy": True, "uncertainty_ms": 10}
        ),
        utcnow=now,
        stop=asyncio.Event(),
    )
    engine = ArbitrageEngine(collector)

    async def save(key, payload, *args):
        return safe_json({**payload, "id": str(uuid4()), "updated_at": now(), "detected_at": now()})

    engine.store = SimpleNamespace(
        expire_previous_runs=AsyncMock(), transition=AsyncMock(), save=save
    )
    samples, latencies, ages = [], [], []
    calculated_latencies, calculated_ages = [], []
    completed_latencies, completed_ages = [], []
    optimizer_calls = 0
    original = engine.evaluate_pair

    async def measured(buy, sell, fx, decision):
        began = elapsed_seconds()
        calls_before = optimizer_calls
        evaluated_before = engine.metrics["evaluations"]
        await original(buy, sell, fx, decision)
        duration_ms = (elapsed_seconds() - began) * 1000
        receipt_age = max(
            (collector.utcnow() - collector.cache.books[m].received_at).total_seconds()
            for m in (buy, sell)
        )
        latencies.append(duration_ms)
        ages.append(receipt_age)
        if optimizer_calls > calls_before:
            calculated_latencies.append(duration_ms)
            calculated_ages.append(receipt_age)
        if engine.metrics["evaluations"] > evaluated_before:
            completed_latencies.append(duration_ms)
            completed_ages.append(receipt_age)

    numeric_results = {}
    original_optimizer = engine_module.optimize_route

    def observed_optimizer(buy, sell, **kwargs):
        nonlocal optimizer_calls
        optimizer_calls += 1
        result = original_optimizer(buy, sell, **kwargs)
        best = result.best
        numeric_results[f"{buy.market_id}:{sell.market_id}"] = {
            name: str(getattr(best, name))
            if best is not None and getattr(best, name, None) is not None
            else None
            for name in (
                "buy_quantity",
                "sell_quantity",
                "buy_vwap",
                "sell_vwap",
                "net_profit_cad",
                "net_profit_quote",
                "acquisition_cost_cad",
            )
        }
        return result

    engine_module.optimize_route = observed_optimizer
    engine.evaluate_pair = measured
    task = asyncio.create_task(engine.run())
    duration = int(os.environ.get("BENCHMARK_SECONDS", "60"))
    if not 1 <= duration <= 300:
        raise ValueError("Synthetic benchmark duration must be 1..300 seconds")
    next_update = 0
    while elapsed_seconds() - start < duration:
        if elapsed_seconds() >= next_update:
            for m, b in list(collector.cache.books.items()):
                # New independent observations, with identical executable prices.
                collector.cache.books[m] = b.model_copy(
                    update={"received_at": now(), "exchange_timestamp": now()}
                )
                engine.changed(collector.cache.books[m])
            next_update = elapsed_seconds() + 1
        samples.append({"elapsed": elapsed_seconds() - start, "backlog": len(engine.jobs)})
        await asyncio.sleep(0.1)
    collector.stop.set()
    await task

    def stats(values):
        values = sorted(values)
        n = len(values)
        import math

        return {
            "n": n,
            **{
                k: values[max(0, math.ceil(n * p) - 1)] if n else None
                for k, p in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1))
            },
        }

    print(
        json.dumps(
            {
                "kind": "SYNTHETIC_OFFLINE_IDENTICAL_MARKET_SET",
                "fixture_version": "deterministic-ids-v4-projection-completion-trace",
                "market_set_sha256": hashlib.sha256(
                    json.dumps(spec, sort_keys=True).encode()
                ).hexdigest(),
                "monitored": len(books),
                "seconds": elapsed_seconds() - start,
                "cpu_seconds": time.process_time() - cpu_start,
                "backlog": stats([r["backlog"] for r in samples]),
                "route_call_ms": stats(latencies),
                "receive_to_evaluation_seconds": stats(ages),
                "calculated_route_ms": stats(calculated_latencies),
                "calculated_receive_to_evaluation_seconds": stats(calculated_ages),
                "completed_evaluation_ms": stats(completed_latencies),
                "completed_receive_to_evaluation_seconds": stats(completed_ages),
                "optimizer_calls": optimizer_calls,
                "diagnostics": engine.diagnostics(),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "numeric_results": numeric_results,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
