"""Durable notification state, separate from orders and append-only financial history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arbiq.db.base import Base, Record


class TelegramChallenge(Record, Base):
    __tablename__ = "telegram_challenges"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    requested_at: Mapped[datetime | None]
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_username: Mapped[str | None] = mapped_column(String(100))
    consumed_at: Mapped[datetime | None]


class TelegramBinding(Record, Base):
    __tablename__ = "telegram_bindings"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime]
    paused: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None]


class NotificationWorkerState(Base):
    __tablename__ = "notification_worker_state"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    update_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    heartbeat_at: Mapped[datetime]
    healthy: Mapped[bool] = mapped_column(default=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


