#!/usr/bin/env python3
"""Profile one bounded read-only real opportunity query in an isolated process.

Run from the workspace. Existing backend credentials remain inside its container;
only SQL column/path definitions and public scalar size/timing measurements cross
the process boundary. No restart, migration, external API or database write occurs.
"""

import argparse
import base64
import json
import subprocess
from decimal import Decimal

from arbiq.db.models import ArbitrageRoute
from arbiq.opportunities.api import list_route_query
from sqlalchemy.dialects import postgresql

PROFILE = r"""
import asyncio,base64,gc,json,resource,time
from sqlalchemy import select,text
from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine
from arbiq.core.config import get_settings
from arbiq.db.models import ArbitrageRoute

def rss():
    with open('/proc/self/status') as stream:
        return int(next(line for line in stream if line.startswith('VmRSS:')).split()[1])*1024

async def main():
    engine=create_async_engine(get_settings().database_url.get_secret_value(),
        hide_parameters=True,pool_size=1,max_overflow=0)
    try:
        async with async_sessionmaker(engine,expire_on_commit=False)() as db:
            await db.execute(text('SET TRANSACTION READ ONLY'))
            gc.collect()
            before=rss()
            started=time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
            if MODE=='full':
                rows=(await db.execute(select(ArbitrageRoute).order_by(
                    ArbitrageRoute.updated_at.desc()).limit(LIMIT))).scalars().all()
            else:
                sql=base64.b64decode(SQL).decode()
                rows=(await db.execute(text(sql))).all()
            elapsed=time.clock_gettime(time.CLOCK_MONOTONIC_RAW)-started
            held=rss()
            # Encode only one row at a time, after the held-RSS measurement.
            payload_bytes=sum(len(json.dumps(row.payload,separators=(',',':')).encode()) for row in rows)
            result={'mode':MODE,'kind':'READ_ONLY_GENUINE_RETAINED_ROUTE_PROFILE',
                'routes':len(rows),'rss_before_bytes':before,'rss_held_bytes':held,
                'rss_query_growth_bytes':held-before,
                'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                'query_seconds':round(elapsed,6),'decoded_payload_json_bytes':payload_bytes,
                'application_database_writes':0,'external_requests':0}
            del rows
            gc.collect()
            result['rss_after_release_bytes']=rss()
            print(json.dumps(result))
    finally:
        await engine.dispose()
asyncio.run(main())
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "projected"), required=True)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.limit <= 500:
        raise SystemExit("Read profile is bounded to 1..500 retained routes")
    query = (
        list_route_query(Decimal(1000))
        .order_by(ArbitrageRoute.updated_at.desc())
        .limit(args.limit)
    )
    sql = str(
        query.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    encoded = base64.b64encode(sql.encode()).decode()
    program = f"MODE={args.mode!r}\nLIMIT={args.limit}\nSQL={encoded!r}\n" + PROFILE
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend", "python", "-"],
        input=program,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise SystemExit(
            "Read-only isolated profile failed; credentials and raw errors suppressed"
        )
    value = json.loads(result.stdout)
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
