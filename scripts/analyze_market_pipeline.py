"""Summarize actual read-only pipeline JSONL without inventing absent timing stages."""

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stats(values):
    values = sorted(values)
    n = len(values)

    def rank(p):
        return round(values[max(0, math.ceil(n * p) - 1)], 3) if n else None

    return {
        "sample_count": n,
        "p50": rank(0.5),
        "p95": rank(0.95) if n >= 20 else None,
        "p99": rank(0.99) if n >= 100 else None,
        "maximum": rank(1),
    }


def analyze(filename, market_scope=None):
    rows = [json.loads(line) for line in filename.read_text().splitlines()]
    groups = defaultdict(lambda: defaultdict(list))
    seen, market_set = set(), set()
    counts, negatives = Counter(), []
    health_first, health_last = {}, {}
    for record in rows:
        for row in record.get("health", []):
            health_first.setdefault(row["slug"], row)
            health_last[row["slug"]] = row
        clock = record.get("clock", {}).get("value", {})
        if not clock.get("trusted") or not clock.get("utc_now"):
            counts["untrusted_rounds"] += 1
            continue
        now = stamp(clock["utc_now"])
        for book in record.get("books", []):
            if "measurement_time_basis" in book and not book.get("measured_at_utc"):
                counts["book_measurement_clock_unavailable"] += 1
                continue
            venue, symbol = book["exchange"], book["symbol"]
            if market_scope is not None and (venue, symbol) not in market_scope:
                continue
            if book.get("http") != 200 or not book.get("received_at"):
                if book.get("http") == 404:
                    counts["book_probe_unavailable_404"] += 1
                elif book.get("error"):
                    counts["book_probe_transport_errors"] += 1
                else:
                    counts["book_probe_other_http_errors"] += 1
                continue
            market_set.add((venue, symbol))
            counts["book_samples"] += 1
            measured = stamp(book["measured_at_utc"]) if book.get("measured_at_utc") else now
            age = (measured - stamp(book["received_at"])).total_seconds()
            source_age = (
                (measured - stamp(book["exchange_timestamp"])).total_seconds()
                if book.get("exchange_timestamp")
                else None
            )
            if age < 0:
                negatives.append(age)
            if source_age is None:
                counts["missing_source_timestamp"] += 1
            if age > 30 or (source_age is not None and source_age > 30):
                counts["stale_over_30_seconds"] += 1
            if book["state"] == "UNAVAILABLE":
                counts["unavailable_book_samples"] += 1
            scopes = [
                "all",
                "exchange:" + venue,
                "market:" + venue + "|" + symbol,
                "transport:" + ("WebSocket" if book["source"].endswith("_ws") else "REST"),
                "asset:" + symbol.split("/")[0],
            ]
            observation = (venue, symbol, book["received_at"], book["source"])
            for scope in scopes:
                if age >= 0:
                    groups[scope]["book_age_seconds"].append(age)
                if book.get("measurement_uncertainty_ms") is not None:
                    groups[scope]["measurement_uncertainty_ms"].append(
                        book["measurement_uncertainty_ms"]
                    )
                if source_age is not None and source_age >= 0:
                    groups[scope]["source_age_seconds"].append(source_age)
                if observation not in seen and book.get("latency_ms") is not None:
                    groups[scope]["rest_call_ms"].append(book["latency_ms"])
                    pipeline = book.get("pipeline") or {}
                    for field in ("http_body_ms", "adapter_parse_ms", "global_queue_ms"):
                        value = pipeline.get(field)
                        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                            groups[scope][field].append(value)
            seen.add(observation)
    for r in rows:
        metrics = r.get("runtime", {}).get("value", {}).get("scanner", {}).get("metrics", {})
        if metrics.get("pending_comparisons") is not None:
            groups["all"]["comparison_backlog"].append(metrics["pending_comparisons"])
    return {
        "kind": "ACTUAL_PRODUCTION_READONLY_OBSERVATIONS",
        "window_start": rows[0]["clock"]["value"].get("utc_now"),
        "window_end": rows[-1]["clock"]["value"].get("utc_now"),
        "elapsed_seconds": rows[-1]["elapsed_seconds"],
        "runtime_samples": len(rows),
        "book_rounds": sum(bool(r.get("books")) for r in rows),
        "counts": dict(counts),
        "negative_age_samples_excluded": len(negatives),
        "most_negative_age_seconds": min(negatives, default=None),
        "sampling_caveat": (
            "Per-book trusted UTC is projected by observer monotonic RAW when present; "
            "uncertainty includes reference, IPC RTT and drift. Legacy rounds without "
            "measured_at_utc are preliminary. Negative ages are excluded, never clamped. "
            "Repeated age samples are correlated. REST observations are deduplicated; "
            "missed intervening requests are not reconstructed."
        ),
        "market_set": sorted([list(key) for key in market_set]),
        "groups": {
            k: {name: stats(values) for name, values in v.items()} for k, v in groups.items()
        },
        "request_deltas": {
            k: {
                "successful": v["successful_requests"] - health_first[k]["successful_requests"],
                "failed": v["failed_requests"] - health_first[k]["failed_requests"],
                "state": v["state"],
                "error_code": v.get("error_code"),
                "monitored": v["monitored_count"],
            }
            for k, v in health_last.items()
        },
        "last_runtime": rows[-1].get("runtime", {}).get("value"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--market-set", type=Path, help="JSON array of [exchange, symbol] pairs")
    args = parser.parse_args()
    scope = (
        {tuple(pair) for pair in json.loads(args.market_set.read_text())}
        if args.market_set is not None
        else None
    )
    args.output.write_text(json.dumps(analyze(args.input, scope), indent=2))
