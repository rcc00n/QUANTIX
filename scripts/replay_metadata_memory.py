#!/usr/bin/env python3
"""No-network allocator/cache comparison from checksum-verified real metadata."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from arbiq.collector.config import CollectorConfig
from arbiq.connectors.ccxt_public import PublicConnector
from arbiq.connectors.metadata import FIELDS, decode
from arbiq.connectors.registry import capabilities
from arbiq.market_data.discovery import normalize_market, selected_markets
from profile_public_metadata import memory


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.fixture / "manifest.json").read_text())
    assert manifest["kind"] == "ACTUAL_PUBLIC_METADATA_NO_EXECUTIONS"
    config = CollectorConfig()
    connectors, catalogs, selected = {}, {}, {}
    try:
        for slug, digest in manifest["files"].items():
            raw = (args.fixture / f"{slug}.zdata").read_bytes()
            assert hashlib.sha256(raw).hexdigest() == digest
            bundle = decode(raw)
            connector = PublicConnector(slug, config)
            for field in FIELDS:
                if bundle.get(field) is not None:
                    setattr(connector.adapter, field, bundle[field])
            connector.adapter.options = bundle["options"]
            connector.adapter.symbols = sorted(connector.adapter.markets)
            connector.adapter.ids = sorted(connector.adapter.markets_by_id)
            connector.adapter.codes = sorted(connector.adapter.currencies)
            future = asyncio.get_running_loop().create_future()
            future.set_result(connector.adapter.markets)
            connector.adapter.markets_loading = future
            markets = [m for m in connector.adapter.markets.values()
                       if m.get("spot") is True and not m.get("contract")]
            catalogs[slug] = [normalize_market(slug, m, connector.adapter.currencies,
                precision_mode=connector.adapter.precisionMode) for m in markets]
            selected[slug] = [m.native_symbol for m in selected_markets(catalogs[slug], config)]
            capabilities(connector.adapter, markets)
            connectors[slug] = connector
            del raw, bundle, future, markets
        before = memory()
        for slug, connector in connectors.items():
            await connector.archive_metadata()
            await connector.focus_metadata(selected[slug])
        after = memory()
        report = {"kind": "NO_NETWORK_IDENTICAL_REAL_METADATA_REPLAY",
                  "fixture_manifest_sha256": hashlib.sha256(
                      (args.fixture / "manifest.json").read_bytes()).hexdigest(),
                  "python_allocator": os.environ.get("PYTHONMALLOC", "default"),
                  "malloc_arena_max": os.environ.get("MALLOC_ARENA_MAX"),
                  "before": before, "after": after,
                  "rss_saved_bytes": before["Rss"] - after["Rss"],
                  "catalog_count_unchanged": sum(len(m) for m in catalogs.values()),
                  "monitored_unchanged": sum(len(m) for m in selected.values()),
                  "cold_metadata_bytes": sum(c.metadata_archive.compressed_bytes
                                             for c in connectors.values())}
        args.output.write_text(json.dumps(report, indent=2))
        args.output.chmod(0o600)
        print(json.dumps(report))
    finally:
        await asyncio.gather(*(c.close() for c in connectors.values()), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
