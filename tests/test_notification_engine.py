"""SYNTHETIC PostgreSQL alert evaluation tests; no application data or real providers."""

from datetime import timedelta
from uuid import UUID

from arbiq.db.models import ArbitrageEvaluation
from arbiq.notifications.models import AlertDecision, AlertLifecycle, NotificationOutbox
from arbiq.notifications.reports import report, schedule_reports
from arbiq.notifications.service import acknowledge, revoke_pairing
from arbiq.opportunities.contracts import identity_cad
from arbiq.opportunities.storage import OpportunityStore
from arbiq.settings.terminal import (
    NotificationPreferences,
    TerminalSettings,
    save_terminal,
)
from arbiq.signals.engine import cooldown_reason, scan
from sqlalchemy import func, select
from test_arbitrage_engine import NOW
from test_notifications import synthetic_owner, synthetic_pair
from test_stage3_storage import seed_scanner


async def synthetic_source(database_url, *, capital="6.71"):
    engine, sessions, store, scanner, books = await seed_scanner(database_url)
    buy_id, sell_id = books[0].market_id, books[1].market_id
    await scanner.evaluate_pair(buy_id, sell_id, {"CAD": identity_cad()}, NOW)
    key, payload, buy, sell = scanner.store.saved[0]
    buy_rt, buy_market = scanner.market_map[buy_id]
    sell_rt, sell_market = scanner.market_map[sell_id]
    storage = OpportunityStore(sessions, store.run_id)
    body = {
        **payload,
        "acquisition_cost_cad": capital,
        "net_profit_cad": "5",
        "net_return_pct": "10",
        "expires_at": NOW + timedelta(seconds=30),
        "supporting_received_at": NOW.isoformat(),
        "supporting_source_at": NOW.isoformat(),
        "evidence": {
            **payload["evidence"],
            "buy_quality": "HEALTHY",
            "sell_quality": "HEALTHY",
        },
    }

    async def save(capital_value):
        return await storage.save(
            key,
            {**body, "acquisition_cost_cad": capital_value},
            buy,
            sell,
            buy_market,
            sell_market,
            buy_rt.exchange_id,
            sell_rt.exchange_id,
            scanner.fee_rate(buy_market),
            scanner.fee_rate(sell_market),
            NOW,
        )

    saved = await save(capital)
    return engine, sessions, save, UUID(saved["id"])


async def test_small_research_is_retained_but_not_notified_then_eligible_dedup_cooldown(
    database_url,
):
    engine, sessions, save, route_id = await synthetic_source(database_url)
    user_id = None
    try:
        async with sessions() as db:
            user = await synthetic_owner(db)
            user_id = user.id
            await synthetic_pair(db, user, NOW)
            settings = TerminalSettings(
                notifications=NotificationPreferences(
                    telegram_enabled=True,
                    research_enabled=True,
                    quiet_hours_enabled=False,
                )
            )
            await save_terminal(db, user.id, settings, NOW)
            await db.commit()
        async with sessions() as db:
            before = await db.scalar(
                select(func.count())
                .select_from(ArbitrageEvaluation)
                .where(ArbitrageEvaluation.route_id == route_id)
            )
            await scan(db, NOW, clock_trusted=True, app_url="http://synthetic.invalid")
            await db.commit()
            decisions = list(
                await db.scalars(
                    select(AlertDecision).where(AlertDecision.user_id == user_id)
                )
            )
            mine = [d for d in decisions if d.evidence["route_id"] == str(route_id)]
            assert len(mine) == 1 and not mine[0].eligible
            assert "BUY_CAPITAL_BELOW_MINIMUM" in mine[0].reasons
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ArbitrageEvaluation)
                    .where(ArbitrageEvaluation.route_id == route_id)
                )
                == before
            )
        await save("50")
        async with sessions() as db:
            await scan(db, NOW, clock_trusted=True, app_url="http://synthetic.invalid")
            await db.commit()
            rows = list(
                await db.scalars(
                    select(NotificationOutbox)
                    .join(AlertDecision)
                    .where(
                        NotificationOutbox.user_id == user_id,
                        AlertDecision.evidence["route_id"].astext == str(route_id),
                    )
                )
            )
            assert len(rows) == 1 and rows[0].kind == "research"
            assert "RESEARCH ONLY" in rows[0].message
            assert not rows[0].silent
            alert_id = rows[0].alert_id
            await scan(db, NOW, clock_trusted=True, app_url="http://synthetic.invalid")
            await db.commit()
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(NotificationOutbox)
                    .where(NotificationOutbox.alert_id == alert_id)
                )
                == 1
            )
            source = await db.scalar(
                select(ArbitrageEvaluation)
                .where(ArbitrageEvaluation.route_id == route_id)
                .order_by(ArbitrageEvaluation.revision.desc())
                .limit(1)
            )
            assert (
                await cooldown_reason(
                    db, user_id, source, settings.notifications, NOW, False
                )
                == "ROUTE_COOLDOWN"
            )
            with_repeat = await db.scalar(
                select(AlertLifecycle).where(AlertLifecycle.decision_id == alert_id)
            )
            with_repeat.next_repeat_at = NOW + timedelta(seconds=60)
            await acknowledge(db, user_id, alert_id, NOW)
            await db.commit()
            assert (
                with_repeat.acknowledged_at == NOW
                and with_repeat.next_repeat_at is None
            )
            assert rows[0].status == "cancelled"
    finally:
        if user_id:
            async with sessions() as db:
                await revoke_pairing(db, user_id, NOW)
                await db.commit()
        await engine.dispose()


