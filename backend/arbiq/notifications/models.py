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


class AlertDecision(Record, Base):
    """Immutable rules and source references; a decision is never an execution."""

    __tablename__ = "alert_decisions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    evaluation_id: Mapped[UUID] = mapped_column(ForeignKey("arbitrage_evaluations.id"))
    settings_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("terminal_settings_versions.id")
    )
    deduplication_key: Mapped[str] = mapped_column(String(180), unique=True)
    evaluated_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    classification: Mapped[str] = mapped_column(String(24))
    eligible: Mapped[bool]
    emergency: Mapped[bool]
    reasons: Mapped[list] = mapped_column(JSONB)
    rules: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (Index("ix_alert_decisions_user_time", "user_id", "evaluated_at"),)


class AlertLifecycle(Record, Base):
    __tablename__ = "alert_lifecycle"
    decision_id: Mapped[UUID] = mapped_column(ForeignKey("alert_decisions.id"), unique=True)
    acknowledged_at: Mapped[datetime | None]
    acknowledged_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None]
    repeat_count: Mapped[int] = mapped_column(default=0)
    next_repeat_at: Mapped[datetime | None]


class NotificationOutbox(Record, Base):
    __tablename__ = "notification_outbox"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    binding_id: Mapped[UUID] = mapped_column(ForeignKey("telegram_bindings.id"))
    alert_id: Mapped[UUID | None] = mapped_column(ForeignKey("alert_decisions.id"), index=True)
    deduplication_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    silent: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    available_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    delivered_at: Mapped[datetime | None]
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (Index("ix_notification_outbox_due", "status", "available_at"),)


