"""Immutable terminal configuration and research experiment creation receipts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arbiq.db.base import Base, Record


class TerminalSettingsVersion(Record, Base):
    __tablename__ = "terminal_settings_versions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int]
    settings: Mapped[dict] = mapped_column(JSONB)
    effective_at: Mapped[datetime]
    __table_args__ = (UniqueConstraint("user_id", "version"),)


class FundingProposal(Record, Base):
    __tablename__ = "funding_proposals"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime]
    payload: Mapped[dict] = mapped_column(JSONB)


class FundingExperiment(Record, Base):
    __tablename__ = "funding_experiments"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_session_id: Mapped[UUID | None] = mapped_column(ForeignKey("paper_sessions.id"))
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)
