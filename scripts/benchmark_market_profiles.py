"""Compare bounded market profiles with one retained public metadata capture.

Offline only. The fixture contains no quotes, execution outcomes or account details.
"""

import argparse
import json
import sys
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from statistics import median
from time import process_time_ns
from uuid import NAMESPACE_URL, uuid5

import ccxt
from arbiq.collector.config import CollectorConfig, MarketProfile
from arbiq.collector.stream_selection import native_symbols
from arbiq.connectors.metadata import decode
from arbiq.market_data.discovery import normalize_market
from arbiq.opportunities.coverage import allocate, intersections, metrics

ASSETS = ("BTC", "ETH", "SOL", "XRP", "LTC", "ADA", "LINK", "DOGE")


def load_fixture(root):
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["kind"] != "ACTUAL_PUBLIC_METADATA_NO_EXECUTIONS":
        raise ValueError("Expected retained public market metadata")

    catalogs = {}
    for slug, digest in sorted(manifest["files"].items()):
        raw = (root / (slug + ".zdata")).read_bytes()
        if sha256(raw).hexdigest() != digest:
            raise ValueError(f"Catalog digest mismatch for {slug}")
        bundle = decode(raw)
        adapter = getattr(ccxt, slug)()
        identities = {}
        catalogs[slug] = [
            normalize_market(
                slug,
                row,
                bundle["currencies"],
                precision_mode=adapter.precisionMode,
                identity_by_symbol=identities,
            )
            for row in bundle["markets"].values()
            if row.get("spot") is True and not row.get("contract")
        ]
    return catalogs, sha256(manifest_bytes).hexdigest()


def compare(catalogs, policy, calls):
    config = CollectorConfig(market_profile=policy)
    elapsed = []
    explored_bases = set()
    for epoch in range(calls):
        started = process_time_ns()
        selected = allocate(catalogs, config, research_epoch=epoch)
        elapsed.append((process_time_ns() - started) / 1_000_000)
        explored_bases.update(
            market.base.key for markets in selected.values() for market in markets
        )

    tracemalloc.start()
    try:
        first = allocate(catalogs, config)
        _, allocation_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    monitored = {
        slug: [
            replace(
                market, id=uuid5(NAMESPACE_URL, f"offline:{slug}:{market.native_id}")
            )
            for market in markets
        ]
        for slug, markets in first.items()
    }
    count = Counter(
        market.base.symbol for rows in monitored.values() for market in rows
    )
    route_counts = defaultdict(int)
    for (base, _), venues in intersections(monitored).items():
        route_counts[base] += len(venues) * (len(venues) - 1)
    slot_plan = {
        venue: list(
            native_symbols(
                venue,
                monitored,
                config.priority_assets,
                config.websocket_fast_markets_per_exchange,
                profile=policy,
            )
        )
        for venue in ("coinbaseexchange", "okx")
    }
    return {
        "monitored_books": sum(count.values()),
        "distinct_base_symbols": len(count),
        "distinct_base_identities_across_epochs": len(explored_bases),
        "same_quote_intersections": metrics(
            catalogs, monitored
        ).compatible_intersections,
        "per_asset": {
            asset: {
                "books": count[asset],
                "directed_same_quote_routes": route_counts[f"asset:{asset}"],
            }
            for asset in ASSETS
        },
        "planned_slots": slot_plan,
        "process_cpu_ms_median": round(median(elapsed), 3),
        "process_cpu_ms_max": round(max(elapsed), 3),
        "python_allocation_peak_bytes": allocation_peak,
        "allocation_calls": calls,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--calls", type=int, default=15, choices=range(1, 101), metavar="1..100"
    )
    args = parser.parse_args()
    catalogs, digest = load_fixture(args.fixture)
    json.dump(
        {
            "evidence": "retained exchange metadata only",
            "fixture_sha256": digest,
            "adapters": len(catalogs),
            "spot_catalog_rows": sum(map(len, catalogs.values())),
            "coverage": {
                str(profile) if profile else "LEGACY_DEFAULT": compare(
                    catalogs, profile, args.calls
                )
                for profile in (None, *MarketProfile)
            },
        },
        sys.stdout,
        indent=2,
    )
    print()


if __name__ == "__main__":
    main()
