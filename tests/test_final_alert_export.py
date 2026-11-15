"""SYNTHETIC isolated-DB notification export, ownership and historical-cutoff checks."""
# Pytest discovers the explicitly imported shared synthetic fixtures by name.
# ruff: noqa: F811

import json
import zipfile
from datetime import timedelta
from uuid import uuid4

from arbiq.analytics.contracts import ReportPeriod
from arbiq.analytics.export import make_export
from arbiq.db.models import PaperSession, PaperTrade
from arbiq.notifications.models import (
    AlertDecision,
    NotificationEvent,
    NotificationOutbox,
    TelegramBinding,
)
from arbiq.paper_trading.models import PaperControl
from test_analytics_export import rows
from test_paper_execution import NOW, environment  # noqa: F401


async def test_alert_records_export_without_private_destinations_or_future_delivery(environment):
    e = environment
    cutoff = NOW + timedelta(seconds=60)
    async with e.sessions() as db:
        trade = await db.get(PaperTrade, e.ids["trade"])
        binding = TelegramBinding(
            user_id=e.ids["user"], chat_id=int(uuid4().hex[:12], 16),
            telegram_user_id=int(uuid4().hex[:12], 16), approved_at=NOW, created_at=NOW,
        )
        db.add(binding)
        await db.flush()
        decision = AlertDecision(
            user_id=e.ids["user"], evaluation_id=trade.evaluation_id,
            deduplication_key="SYNTHETIC-" + uuid4().hex, evaluated_at=NOW,
            expires_at=NOW + timedelta(minutes=2), classification="RESEARCH_ONLY",
            eligible=False, emergency=False, reasons=["SYNTHETIC_UNVERIFIED"],
            rules={"api_key": "SYNTHETIC-PRIVATE-KEY"}, evidence={"synthetic": True},
            created_at=NOW,
        )
        db.add(decision)
        await db.flush()
        outbox = NotificationOutbox(
            user_id=e.ids["user"], binding_id=binding.id, alert_id=decision.id,
            deduplication_key="SYNTHETIC-" + uuid4().hex,
            kind="research", message="SYNTHETIC-PRIVATE-COMMAND-TEXT", status="delivered",
            available_at=cutoff + timedelta(seconds=1), created_at=NOW,
            expires_at=cutoff + timedelta(minutes=2), attempts=3,
            delivered_at=cutoff + timedelta(seconds=2), provider_message_id="SYNTHETIC-RECEIPT",
        )
        db.add(outbox)
        await db.flush()
        db.add(NotificationEvent(
            user_id=e.ids["user"], outbox_id=outbox.id, alert_id=decision.id,
            event_type="delivery.pending", details={"synthetic": True}, created_at=NOW,
        ))
        await db.commit()
        session = await db.get(PaperSession, e.ids["session"])
        control = await db.get(PaperControl, session.id)
        path, _ = await make_export(
            db, session, control, ReportPeriod(start=NOW, end=cutoff), cutoff,
            {"trusted": True, "synthetic": True},
        )
        decision_id, chat_id = str(decision.id), str(binding.chat_id)
    try:
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None
            signal = next(x for x in rows(z, "signals.csv") if x["id"] == decision_id)
            assert signal["decision"] == "RESEARCH_ONLY:REJECTED"
            assert signal["telegram_delivery_status"] == "no_recorded_delivery"
            out = rows(z, "alert_outbox.csv")[0]
            assert out["status"] == "UNDETERMINED_AT_CUTOFF"
            assert out["delivered_at"] == out["attempts"] == out["available_at"] == ""
            assert len(rows(z, "alert_events.csv")) == 1
            complete = b"".join(z.read(name) for name in z.namelist())
            for value in (chat_id, "SYNTHETIC-PRIVATE-KEY", "SYNTHETIC-RECEIPT",
                          "SYNTHETIC-PRIVATE-COMMAND-TEXT"):
                assert value.encode() not in complete
            config = json.loads(z.read("config_snapshot.json"))
            assert config["notification_delivery_active"] is None
            metrics = json.loads(z.read("metrics.json"))
            assert metrics["notification_counts"]["telegram_alerts_delivered"] == 0
            assert metrics["notification_counts"]["research_decisions"] >= 1
    finally:
        path.unlink(missing_ok=True)
