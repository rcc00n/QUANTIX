"""Simulation controls and causally available retained observations, never real orders."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arbiq.db.base import Base, Record


class PaperControl(Base):
    __tablename__ = "paper_controls"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"), primary_key=True)
    strategy_version_id: Mapped[UUID] = mapped_column(ForeignKey("strategy_versions.id"))
    classification: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))
    settings: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime]
    __table_args__ = (
        CheckConstraint("classification IN ('VALIDATED','RESEARCH')", name="classification"),
        CheckConstraint("mode IN ('PRE_FUNDED','TRANSFER','RESEARCH_SHADOW')", name="mode"),
        CheckConstraint("status IN ('ACTIVE','PAUSED')", name="status"),
        CheckConstraint(
            "(classification='RESEARCH') = (mode='RESEARCH_SHADOW')", name="research_isolation"
        ),
    )


class StrategyHead(Base):
    __tablename__ = "strategy_heads"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    strategy_version_id: Mapped[UUID] = mapped_column(ForeignKey("strategy_versions.id"))
    updated_at: Mapped[datetime]


class ExecutionObservation(Record, Base):
    __tablename__ = "execution_observations"
    trade_id: Mapped[UUID] = mapped_column(ForeignKey("paper_trades.id"), index=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("paper_orders.id"))
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("order_book_snapshots.id"))
    received_at: Mapped[datetime]
    source_at: Mapped[datetime | None]
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("order_id", "snapshot_id"),)


