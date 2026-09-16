from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arbiq.auth.security import hash_token
from arbiq.db.models import AuthSession, User
from arbiq.db.session import get_db

DB = Annotated[AsyncSession, Depends(get_db)]


async def current_user(db: DB, arbiq_session: Annotated[str | None, Cookie()] = None) -> User:
    if not arbiq_session or len(arbiq_session) > 128:
        raise HTTPException(status_code=401, detail="Authentication required")
    user = await db.scalar(
        select(User)
        .join(AuthSession)
        .where(
            AuthSession.token_hash == hash_token(arbiq_session),
            AuthSession.expires_at > datetime.now(UTC),
            AuthSession.revoked_at.is_(None),
            User.active.is_(True),
        )
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    # Authentication performs only reads. Return its scarce pool connection before
    # a handler waits on collector IPC, a singleflight lock or a report job. The
    # application's expire_on_commit=False retains the verified user attributes.
    await db.commit()
    return user


AuthenticatedUser = Annotated[User, Depends(current_user)]
