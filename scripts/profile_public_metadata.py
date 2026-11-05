#!/usr/bin/env python3
"""One real public discovery pass; lossless-cache before/after, never trade traffic."""

import argparse
import asyncio
import ctypes
import gc
import hashlib
import json
import pickle
from pathlib import Path

from arbiq.collector.config import DEFAULT_EXCHANGES, CollectorConfig
from arbiq.connectors.ccxt_public import PublicConnector, classify_error
from arbiq.connectors.metadata import FIELDS
from arbiq.connectors.registry import capabilities
from arbiq.market_data.discovery import normalize_market, selected_markets


def memory():
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        key, *rest = line.split()
        if rest and rest[0].isdigit():
            values[key.rstrip(":")] = int(rest[0]) * 1024
    return values


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchanges", nargs="+", default=DEFAULT_EXCHANGES)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-native", type=Path)
    args = parser.parse_args()
    config = CollectorConfig(max_markets_per_exchange=10, max_concurrency=2)
    connectors, catalogs, selected, snapshots, rows = {}, {}, {}, {}, []
    fixture_manifest = {"kind": "ACTUAL_PUBLIC_METADATA_NO_EXECUTIONS", "files": {}}
    if args.record_native:
        args.record_native.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        for slug in args.exchanges:
            connector = PublicConnector(slug, config)
            connectors[slug] = connector
            try:
                raw = await connector.discover_spot_markets()
                catalog = [normalize_market(slug, m, connector.adapter.currencies,
                            precision_mode=connector.adapter.precisionMode) for m in raw]
                catalogs[slug] = catalog
                selected[slug] = [m.native_symbol for m in selected_markets(catalog, config)]
                snapshots[slug] = pickle.dumps({s: connector.adapter.markets[s]
                                               for s in selected[slug]})
                capabilities(connector.adapter, raw)
                if args.record_native:
                    import zlib
                    bundle = {k: getattr(connector.adapter, k, None) for k in FIELDS}
                    bundle["options"] = connector.adapter.options
                    data = zlib.compress(pickle.dumps(bundle, protocol=5), level=3)
                    target = args.record_native / f"{slug}.zdata"
                    target.write_bytes(data)
                    target.chmod(0o600)
                    fixture_manifest["files"][slug] = hashlib.sha256(data).hexdigest()
                    del bundle, data
                rows.append({"exchange": slug, "discovered": len(catalog),
                             "selected": len(selected[slug]), "status": "real_discovery_ok"})
                del raw, catalog
            except Exception as exc:  # noqa: BLE001 - retain exchange-specific failures in a probe.
                rows.append({"exchange": slug, "status": "unavailable",
                             "error_code": classify_error(exc)})
            print(json.dumps(rows[-1]), flush=True)
        before = memory()
        if args.record_native:
            target = args.record_native / "manifest.json"
            target.write_text(json.dumps(fixture_manifest, indent=2))
            target.chmod(0o600)
        for slug in catalogs:
            connector = connectors[slug]
            await connector.archive_metadata()
            await connector.focus_metadata(selected[slug])
            original = pickle.loads(snapshots[slug])  # Self-produced in-process test data only.
            assert original == connector.adapter.markets, "Metadata roundtrip changed values"
            row = next(r for r in rows if r["exchange"] == slug)
            row["cache"] = connector.metadata_metrics
            try:
                book = await connector.fetch_raw_book(selected[slug][0])
                row["post_focus_public_book"] = bool(book.get("bids") and book.get("asks"))
                del book
            except Exception as exc:  # noqa: BLE001 - a failed public recheck is evidence, not success.
                row["post_focus_public_error"] = classify_error(exc)
            print(json.dumps({"focused": slug, **row["cache"]}), flush=True)
            del original
        after = memory()
        result = {"kind": "ACTUAL_PUBLIC_METADATA_LOSSLESS_COMPACTION",
                  "network_calls": "One normal discovery per venue and one public book recheck",
                  "no_database_or_order_calls": True, "synthetic": False,
                  "catalog_count_unchanged": sum(len(c) for c in catalogs.values()),
                  "selected_books_unchanged": sum(len(s) for s in selected.values()),
                  "before": before, "after": after,
                  "rss_saved_bytes": before["Rss"] - after["Rss"], "venues": rows}
        print(json.dumps({k: v for k, v in result.items() if k != "venues"}), flush=True)
        args.output.write_text(json.dumps(result, indent=2))
        args.output.chmod(0o600)
    finally:
        await asyncio.gather(*(c.close() for c in connectors.values()), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
