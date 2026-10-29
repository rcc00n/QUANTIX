"""Authenticated explanatory reads; inputs come only from retained application data."""

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from arbiq.ai.contracts import ExplainRequest
from arbiq.ai.explanations import build_report
from arbiq.ai.providers import NvidiaProvider, fallback, initialize_ai
from arbiq.analytics.service import analytics, owned_session, period_for
from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import ArbitrageEvaluation, SystemEvent, User
from arbiq.market_data.api import utc_reference

router = APIRouter(prefix="/ai", tags=["optional explanatory analysis"])


@router.get("/status")
async def ai_status(request: Request, user: AuthenticatedUser):
    settings = request.app.state.settings
    if not hasattr(request.app.state, "ai_provider"):
        initialize_ai(request.app)
    provider = request.app.state.ai_provider
    reason = "AI_DISABLED"
    if isinstance(provider, NvidiaProvider):
        now, clock = await utc_reference(request)
        reason = provider.disabled_reason(now if clock.get("trusted") else None)
    return {
        "enabled": settings.ai_enabled,
        "provider": "nvidia" if settings.ai_enabled else "none",
        "free_allowance_authorized": getattr(settings, "ai_free_allowance_authorized", False),
        "external_calls_active": reason is None,
        "reason": reason,
        "deterministic_fallback": True,
        "mode": "explanations_only",
        "capabilities": ["retained_opportunity", "owned_paper_report"],
        "financial_authority": False,
        "billing": "No paid usage authorized; explicit expiring free allowance required",
    }


