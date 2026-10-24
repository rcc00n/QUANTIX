"""SYNTHETIC UTC corrections must not roll back logical settings versions."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from arbiq.db.models import User
from arbiq.settings.terminal import (
    TerminalSettings,
    read_effective_terminal,
    save_terminal,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def test_latest_version_survives_small_backward_utc_correction(database_url):
    engine = create_async_engine(database_url)
    now = datetime(2025, 1, 1, tzinfo=UTC)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = User(
                username="synthetic-settings-order-" + uuid4().hex,
                password_hash="SYNTHETIC-not-for-auth",
            )
            db.add(user)
            await db.flush()
            await save_terminal(db, user.id, TerminalSettings(), now, 0)
            await save_terminal(
                db, user.id, TerminalSettings(), now - timedelta(milliseconds=50), 1
            )
            row, _ = await read_effective_terminal(db, user.id)
            assert row.version == 2
            third = await save_terminal(db, user.id, TerminalSettings(), now, 2)
            assert third.version == 3
            await db.commit()
    finally:
        await engine.dispose()
