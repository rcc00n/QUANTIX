#!/usr/bin/env python3
"""Replay route admission on an existing Stage 8C catalog ZIP and book trace."""

import argparse
import csv
import io
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from zipfile import ZipFile

from arbiq.opportunities.comparison_queue import ComparisonQueue
from arbiq.opportunities.coverage import venue_group


def csv_rows(archive, name):
    with io.TextIOWrapper(archive.open(name), encoding="utf8", newline="") as handle:
        yield from csv.DictReader(handle)


def read_input(archive_name, trace_name):
    grouped, markets, labels = defaultdict(set), {}, {}
    with ZipFile(archive_name) as archive:
        venues = {
            row["id"]: row["slug"]
            for row in csv_rows(archive, "exchanges/exchanges.csv")
        }
        for row in csv_rows(archive, "exchanges/markets.csv"):
            info = json.loads(row["metadata_json"])
            if not all(
                info.get(k)
                for k in (
                    "monitored",
                    "base_identity_verified",
                    "quote_identity_verified",
                )
            ):
                continue
            mid = UUID(row["id"])
            slug = venues[row["exchange_id"]]
            market = SimpleNamespace(
                base=SimpleNamespace(key=row["base_asset_id"]),
                quote=SimpleNamespace(key=row["quote_asset_id"]),
            )
            markets[mid] = (SimpleNamespace(slug=slug), market)
            grouped[market.base.key].add(mid)
            labels[slug, row["native_symbol"]] = mid
    trace, previous = [], {}
    for line in Path(trace_name).read_text().splitlines():
        tick = json.loads(line)
        changed = set()
        for book in tick["books"]:
            mid = labels.get((book["venue"], book["market"]))
            if mid is None:
                continue
            capture = (
                book["source"],
                book["receipt"],
                book["exchange_time"],
                book["state"],
            )
            if previous.get(mid) != capture:
                changed.add(mid)
                previous[mid] = capture
        trace.append(changed)
    return grouped, markets, trace


def same_quote(markets, a, b):
    return markets[a][1].quote.key == markets[b][1].quote.key


def base_sweeps(grouped, markets, trace):
    output, examined = [], 0
    for changed in trace:
        ready = set()
        for base in sorted({markets[mid][1].base.key for mid in changed}):
            ids = sorted(grouped[base])
            for buy in ids:
                for sell in ids:
                    examined += 1
                    if venue_group(markets[buy][0].slug) == venue_group(
                        markets[sell][0].slug
                    ):
                        continue
                    if same_quote(markets, buy, sell):
                        ready.add((buy, sell))
        output.append(ready)
    return output, examined


def market_sweeps(grouped, markets, trace):
    queue = ComparisonQueue()
    queue.rebuild(markets, grouped, 0)
    output, examined = [], 0
    for index, changed in enumerate(trace):
        queue.submit(sorted(changed), index)
        arrived = set()
        while queue.deferred:
            checked, _, _ = queue.admit(
                2000, 1024, lambda pair: same_quote(markets, *pair)
            )
            examined += checked
            arrived.update(queue.ready)
            queue.ready.clear()
        output.append(arrived)
    return output, examined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("trace")
    parser.add_argument("--rounds", type=int, default=9)
    args = parser.parse_args()
    grouped, markets, trace = read_input(args.archive, args.trace)
    metrics, output = {}, {}
    for name, method in (
        ("stage8c_base_sweep", base_sweeps),
        ("stage10_market_sweep", market_sweeps),
    ):
        cpu = []
        for _ in range(args.rounds):
            start = time.process_time()
            results, examined = method(grouped, markets, trace)
            cpu.append(1000 * (time.process_time() - start))
        output[name] = results
        metrics[name] = {
            "kernel_median_cpu_ms": statistics.median(cpu),
            "iterations": examined,
            "distinct_jobs_per_window_total": sum(map(len, results)),
        }
    baseline, candidate = output["stage8c_base_sweep"], output["stage10_market_sweep"]
    for changed, original, improved in zip(trace, baseline, candidate, strict=True):
        assert {pair for pair in original if changed.intersection(pair)} == improved
    print(
        json.dumps(
            {
                "archive_selected_books": len(markets),
                "trace_windows": len(trace),
                "observed_capture_changes": sum(map(len, trace)),
                "cpu_runs": args.rounds,
                "scope": "same-quote admission only, no pricing, FX, IPC, SQL or network",
                **metrics,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
