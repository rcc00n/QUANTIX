from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from arbiq.core.config import Settings
from arbiq.db.models import AuthSession, SystemEvent, User


async def provision_operator(db: AsyncSession, config: Settings) -> User:
    """Rename the existing owner in place, preserving financial FKs and revoking sessions."""
    await db.execute(text("SELECT pg_advisory_xact_lock(731092)"))
    user = await db.scalar(select(User).where(User.username == config.admin_username))
    previous = None
    if config.admin_previous_username and config.admin_previous_username != config.admin_username:
        previous = await db.scalar(
            select(User).where(User.username == config.admin_previous_username)
        )
    renamed = False
    if user is None and previous is not None:
        user = previous
        user.username = config.admin_username
        renamed = True
    elif previous is not None and user is not None and previous.id != user.id:
        # Preserve history, but retire the former owner login explicitly.
        previous.active = False
        await db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == previous.id)
            .values(revoked_at=datetime.now(UTC))
        )
        db.add(
            SystemEvent(
                event_type="auth.previous_operator_retired",
                severity="info",
                actor_user_id=previous.id,
                message="Previous operator login retired",
                details={},
            )
        )
    if user is None:
        user = User(
            username=config.admin_username,
            password_hash=config.admin_password_hash.get_secret_value(),
        )
        db.add(user)
        await db.flush()
        db.add(
            SystemEvent(
                event_type="auth.admin_created",
                severity="info",
                actor_user_id=user.id,
                message="Local operator provisioned",
                details={},
            )
        )
    elif renamed or user.password_hash != config.admin_password_hash.get_secret_value():
        user.password_hash = config.admin_password_hash.get_secret_value()
        user.active = True
        await db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id)
            .values(revoked_at=datetime.now(UTC))
        )
        db.add(
            SystemEvent(
                event_type="auth.operator_rotated",
                severity="info",
                actor_user_id=user.id,
                message="Operator credential rotated and prior sessions revoked",
                details={},
            )
        )
    return user
