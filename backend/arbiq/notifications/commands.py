"""Private paired-owner commands backed by application state, not external AI."""

from datetime import timedelta

from sqlalchemy import select

from arbiq.db.models import ArbitrageRoute, PaperSession, User
from arbiq.notifications.messages import value
from arbiq.notifications.models import NotificationWorkerState, TelegramBinding
from arbiq.notifications.reports import report
from arbiq.notifications.service import enqueue, request_pairing, set_paused
from arbiq.portfolio.models import PortfolioCheckpoint
from arbiq.settings.terminal import read_effective_terminal

HELP = (
    "QUANTIX — PAPER ONLY\n/status system state\n/radar retained opportunities\n"
    "/paper simulated performance\n/portfolio independent portfolios\n"
    "/alerts notification policy\n/pause notifications\n/resume notifications\n/help commands\n"
    "Manage pairing, acknowledgment and rechecks in the authenticated dashboard."
)


async def handle_update(db, update, now, *, clock_trusted, app_url):
    msg = update.get("message", {})
    chat, sender = msg.get("chat", {}), msg.get("from", {})
    if chat.get("type") != "private" or chat.get("id") != sender.get("id") or sender.get("is_bot"):
        return
    text = msg.get("text", "")
    if not isinstance(text, str) or not text.startswith("/") or len(text) > 256:
        return
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    if command == "/start" and len(parts) == 2 and clock_trusted:
        await request_pairing(db, parts[1], chat, sender, now)
        # No private data or unsolicited outbound reply before owner dashboard approval.
        return
    binding = await db.scalar(
        select(TelegramBinding)
        .join(User)
        .where(
            TelegramBinding.chat_id == chat["id"],
            TelegramBinding.telegram_user_id == sender["id"],
            TelegramBinding.revoked_at.is_(None),
            User.active.is_(True),
        )
    )
    if binding is None:
        return
    _, config = await read_effective_terminal(db, binding.user_id)
    if command in ("/start", "/help"):
        response = HELP
    elif command == "/pause":
        await set_paused(db, binding.user_id, True)
        response = "QUANTIX notifications paused. Paper trading and market monitoring continue."
    elif command == "/resume":
        await set_paused(db, binding.user_id, False)
        response = "QUANTIX notifications resumed under saved policy; no real trading is enabled."
    elif command == "/alerts":
        p = config.notifications
        response = (
            f"QUANTIX alert policy\nTelegram enabled: {p.telegram_enabled}; paused: "
            f"{binding.paused}\n"
            f"Minimum Buy Capital C${p.minimum_buy_capital_cad}; profit C${p.minimum_profit_cad}; "
            f"return {p.minimum_return_pct}% (ALL required)\nResearch: {p.research_enabled}\n"
            f"Quiet hours {p.quiet_start}–{p.quiet_end} America/Edmonton ({p.quiet_delivery})\n"
            f"Emergency: {p.emergency_alerts}; push/voice disabled, paid access not authorized."
        )
    elif command == "/status":
        state = await db.get(NotificationWorkerState, "telegram")
        latest = await db.scalar(
            select(ArbitrageRoute).order_by(ArbitrageRoute.updated_at.desc()).limit(1)
        )
        response = (
            f"QUANTIX — PAPER ONLY\nTrusted notification UTC: {clock_trusted}\n"
            f"Telegram worker: {'healthy' if state and state.healthy else 'degraded'}\n"
            "Last persisted scanner update: "
            f"{latest.updated_at.isoformat() if latest else 'unavailable'}\n"
            "Retained scanner time does not establish live freshness. Eligibility and "
            "account fees remain unverified."
        )
    elif command == "/radar":
        routes = list(
            await db.scalars(
                select(ArbitrageRoute).order_by(ArbitrageRoute.updated_at.desc()).limit(3)
            )
        )
        response = "QUANTIX — RETAINED OBSERVATIONS\nPrices may no longer be executable.\n"
        response += (
            "\n".join(
                f"{r.payload.get('base_asset')} {r.payload.get('buy_exchange')} → "
                f"{r.payload.get('sell_exchange')}: "
                f"C${value(r.payload.get('net_profit_cad'))} estimated; {r.status}; "
                f"{r.updated_at.isoformat()}"
                for r in routes
            )
            or "No observations available."
        )
    elif command == "/paper":
        try:
            response = await report(db, binding.user_id, now - timedelta(days=1), now)
        except ValueError:
            response = (
                "Paper report exceeds bounded Telegram output. Use dashboard and analytical export."
            )
    elif command == "/portfolio":
        sessions = list(
            await db.scalars(
                select(PaperSession)
                .where(PaperSession.user_id == binding.user_id)
                .order_by(PaperSession.started_at.desc())
                .limit(10)
            )
        )
        response = "QUANTIX — INDEPENDENT PAPER PORTFOLIOS\n"
        for session in sessions:
            checkpoint = await db.scalar(
                select(PortfolioCheckpoint)
                .where(PortfolioCheckpoint.session_id == session.id)
                .order_by(PortfolioCheckpoint.as_of.desc())
                .limit(1)
            )
            p = checkpoint.payload if checkpoint else {}
            response += (
                f"\n{session.name[:80]}: equity C${value(p.get('equity_cad'))}, "
                f"simulated P&L C${value(p.get('net_simulated_pnl_cad'))}; "
                f"as of {checkpoint.as_of.isoformat() if checkpoint else 'unavailable'}"
            )
        if not sessions:
            response += "No funded paper portfolios."
        response += (
            "\nUp to 10 most recent independent portfolios shown; use dashboard for full history."
        )
    else:
        response = HELP
    response += f"\n{app_url.rstrip('/')}"
    await enqueue(
        db,
        binding,
        key=f"command:{update['update_id']}",
        kind="command",
        message=response,
        now=now,
        expires_at=now + timedelta(minutes=5),
    )
