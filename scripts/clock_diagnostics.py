#!/usr/bin/env python3
"""Read-only UTC/elapsed diagnostic. Public observations, no settings/secrets loaded."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime

import aiohttp
from arbiq.core.clock import TrustedClock, elapsed_seconds


async def diagnose(rounds: int, interval: float) -> None:
    clock = TrustedClock()
    started_raw, started_adjusted, started_wall = (
        elapsed_seconds(),
        time.monotonic(),
        datetime.now(UTC),
    )
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=4)
    ) as session:
        for index in range(rounds):
            await clock.synchronize(session)
            snapshot = clock.snapshot()
            snapshot["measurement"] = index + 1
            snapshot["elapsed_raw_seconds"] = elapsed_seconds() - started_raw
            snapshot["elapsed_adjusted_seconds"] = time.monotonic() - started_adjusted
            snapshot["elapsed_wall_seconds"] = (
                datetime.now(UTC) - started_wall
            ).total_seconds()
            print(json.dumps(snapshot), flush=True)
            if index + 1 < rounds:
                for _ in range(int(interval)):
                    await asyncio.sleep(1)
                    clock.observe_wall()
                await asyncio.sleep(interval - int(interval))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, choices=range(2, 121), default=6)
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args()
    if not 5 <= args.interval <= 60:
        parser.error("--interval must be between5 and60 seconds")
    asyncio.run(diagnose(args.rounds, args.interval))
