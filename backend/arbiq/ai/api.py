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


@router.post("/explain")
async def explain(body: ExplainRequest, request: Request, db: DB, user: AuthenticatedUser):
    if not hasattr(request.app.state, "ai_provider"):
        initialize_ai(request.app)
    gate = request.app.state.ai_gate
    if gate.locked():
        raise HTTPException(429, "Explanation busy; retry shortly", headers={"Retry-After": "2"})
    async with gate:
        source_id = body.opportunity_id or body.session_id
        now, clock = await utc_reference(request)
        if body.opportunity_id:
            evaluation = await db.scalar(
                select(ArbitrageEvaluation)
                .where(ArbitrageEvaluation.route_id == body.opportunity_id)
                .order_by(ArbitrageEvaluation.revision.desc())
                .limit(1)
            )
            if evaluation is None:
                raise HTTPException(404, "Retained opportunity evaluation not found")
            payload, source_type = evaluation.result, "opportunity"
            attribution = {
                "evaluation_id": str(evaluation.id),
                "evaluated_at": evaluation.evaluated_at.isoformat(),
                "current_quote": False,
            }
        else:
            session, control = await owned_session(db, user.id, body.session_id)
            period = period_for(session, None, None, now)
            payload = await analytics(db, session, control, period)
            source_type = "paper_report"
            attribution = {
                "period": period.model_dump(mode="json"),
                "classification": control.classification,
                "current_quote": False,
            }
        try:
            report = build_report(source_type, payload)
        except (ValueError, ArithmeticError, TypeError):
            raise HTTPException(409, "Retained numeric explanation inputs unavailable") from None
        provider = request.app.state.ai_provider
        reason = None
        cached = None
        if isinstance(provider, NvidiaProvider):
            reason = provider.disabled_reason(now if clock.get("trusted") else None)
            cached = provider.cached_explanation(report, now=now) if reason is None else None
            if reason is None and cached is None:
                # Serialize per owner and count attempts durably before any network
                # call. Restart cannot reset this safety quota. No response text is
                # retained and no financial model is ever modified by this module.
                await db.scalar(select(User).where(User.id == user.id).with_for_update())
                since = now.replace(hour=0, minute=0, second=0, microsecond=0)
                attempts = await db.scalar(
                    select(func.count())
                    .select_from(SystemEvent)
                    .where(
                        SystemEvent.actor_user_id == user.id,
                        SystemEvent.event_type == "ai.provider_attempt",
                        SystemEvent.created_at >= since,
                    )
                )
                last = await db.scalar(
                    select(func.max(SystemEvent.created_at)).where(
                        SystemEvent.actor_user_id == user.id,
                        SystemEvent.event_type == "ai.provider_attempt",
                    )
                )
                settings = request.app.state.settings
                if attempts >= settings.ai_max_requests_per_day:
                    reason = "AI_DAILY_LIMIT"
                elif last and now < last + timedelta(
                    seconds=settings.ai_min_request_interval_seconds
                ):
                    reason = "AI_RATE_LIMIT"
                else:
                    db.add(
                        SystemEvent(
                            actor_user_id=user.id,
                            event_type="ai.provider_attempt",
                            severity="info",
                            message="Owner requested optional free-allowance explanation",
                            created_at=now,
                            details={"source_type": source_type},
                        )
                    )
                    await db.commit()
        if cached is not None:
            value = cached
        elif reason:
            value = fallback(report, status="unavailable", provider="nvidia", reason=reason)
        else:
            value = await provider.explain(report, now=now)
        return {
            **value.model_dump(mode="json"),
            "source_id": str(source_id),
            "source_type": source_type,
            "attribution": attribution,
            "classification": report.classification,
            "limitations": [
                "Explanation has no financial decision authority",
                "No prices, fills, balances or accounting entries are generated",
            ],
        }
