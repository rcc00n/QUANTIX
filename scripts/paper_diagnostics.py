"""Read actual local paper state; no fixtures, funding or execution writes."""

import asyncio
import json

import aiohttp
from arbiq.core.config import get_settings
from arbiq.db.models import ActualTrade, PaperFill, PaperSession, User
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.analytics import performance
from arbiq.portfolio.models import DailyAccountingSnapshot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def main():
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(), hide_parameters=True
    )
    result = {
        "schema_version": "paper-diagnostics-v1",
        "kind": "ACTUAL_LOCAL_VIRTUAL_STATE",
        "paper_only": True,
        "sessions": [],
    }
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            rows = (
                await db.execute(
                    select(PaperSession, PaperControl)
                    .join(PaperControl)
                    .join(User)
                    .where(User.username == settings.admin_username)
                    .order_by(PaperSession.started_at)
                )
            ).all()
            result["actual_trade_records"] = await db.scalar(
                select(func.count()).select_from(ActualTrade)
            )
            result["paper_fill_records"] = await db.scalar(
                select(func.count()).select_from(PaperFill)
            )
            result["daily_snapshot_records"] = await db.scalar(
                select(func.count()).select_from(DailyAccountingSnapshot)
            )
            async with aiohttp.ClientSession(
                base_url=settings.collector_url,
                headers={
                    "Authorization": "Bearer "
                    + settings.collector_internal_token.get_secret_value()
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as client:
                async with client.get("/paper/status") as response:
                    result["coordinator"] = await response.json()
                async with client.get("/engine/diagnostics") as response:
                    result["scanner"] = await response.json()
                for session, control in rows:
                    async with client.get(
                        f"/paper/portfolios/{session.id}"
                    ) as response:
                        value = await response.json()
                    activity = await performance(
                        db,
                        session.id,
                        research_only=control.classification == "RESEARCH",
                    )
                    result["sessions"].append(
                        {
                            "id": str(session.id),
                            "name": session.name,
                            "classification": control.classification,
                            "strategy_version_id": str(control.strategy_version_id),
                            "portfolio": value,
                            "performance": activity,
                        }
                    )
        print(json.dumps(result, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
