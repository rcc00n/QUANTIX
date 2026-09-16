import asyncio
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, select, text, update

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.auth.security import COOKIE_NAME, hash_token, verify_password
from arbiq.db.models import AuthSession, LoginAttempt, SystemEvent, User

router = APIRouter(prefix="/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=256)


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response, db: DB) -> dict:
    settings = request.app.state.settings
    now = datetime.now(UTC)
    # All BFF requests share an internal address. The account lock also prevents
    # concurrent brute-force attempts bypassing the persisted rate limiter.
    client_hash = hash_token(request.client.host if request.client else "unknown")
    await db.execute(text("SELECT pg_advisory_xact_lock(731091)"))
    recent_failures = await db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.created_at >= now - timedelta(minutes=10),
            LoginAttempt.success.is_(False),
            (LoginAttempt.client_hash == client_hash) | (LoginAttempt.username == payload.username),
        )
    )
    if recent_failures >= 10:
        raise HTTPException(
            429, "Too many login attempts; try again in 10 minutes", headers={"Retry-After": "600"}
        )
    user = await db.scalar(select(User).where(User.username == payload.username))
    stored = user.password_hash if user else settings.admin_password_hash.get_secret_value()
    valid = await asyncio.to_thread(verify_password, payload.password.get_secret_value(), stored)
    success = bool(valid and user and user.active)
    db.add(LoginAttempt(username=payload.username, client_hash=client_hash, success=success))
    db.add(
        SystemEvent(
            event_type="auth.login_succeeded" if success else "auth.login_failed",
            severity="info" if success else "warning",
            actor_user_id=user.id if success else None,
            message="Dashboard login accepted" if success else "Dashboard login rejected",
            details={},
        )
    )
    if not success:
        await db.commit()
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    await db.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"username": user.username, "mode": "paper"}


