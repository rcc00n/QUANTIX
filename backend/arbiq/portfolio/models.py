"""Stage 4 accounting projections and immutable analytical evidence.

Physical quantities are always reconstructible from the existing immutable ledger.
A position is a transactionally maintained cost-basis cache, never a second ledger.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arbiq.db.base import Base, Record


class PortfolioPosition(Record, Base):
    __tablename__ = "portfolio_positions"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    exchange_id: Mapped[UUID] = mapped_column(ForeignKey("exchanges.id"))
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    quantity: Mapped[Decimal] = mapped_column(default=Decimal(0))
    reserved_quantity: Mapped[Decimal] = mapped_column(default=Decimal(0))
    cost_basis_cad: Mapped[Decimal | None]
    updated_at: Mapped[datetime]
    __table_args__ = (
        UniqueConstraint("session_id", "exchange_id", "asset_id"),
        CheckConstraint(
            "quantity >= 0 AND reserved_quantity >= 0 AND reserved_quantity <= quantity",
            name="inventory",
        ),
        CheckConstraint("cost_basis_cad >= 0", name="basis"),
    )


class AccountingMovement(Record, Base):
    __tablename__ = "accounting_movements"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    journal_id: Mapped[UUID] = mapped_column(ForeignKey("journals.id"), unique=True)
    event_type: Mapped[str] = mapped_column(String(40))
    effective_at: Mapped[datetime]
    realized_trading_cad: Mapped[Decimal | None]
    realized_fx_cad: Mapped[Decimal | None]
    fees_cad: Mapped[Decimal | None]
    expenses_cad: Mapped[Decimal | None]
    external_flow_cad: Mapped[Decimal | None]
    details: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (Index("ix_accounting_session_effective", "session_id", "effective_at"),)


class PortfolioCheckpoint(Record, Base):
    __tablename__ = "portfolio_checkpoints"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    as_of: Mapped[datetime]
    payload: Mapped[dict] = mapped_column(JSONB)
    calculation_version: Mapped[str] = mapped_column(String(40))
    __table_args__ = (Index("ix_portfolio_checkpoint_session_time", "session_id", "as_of"),)


class DailyAccountingSnapshot(Record, Base):
    __tablename__ = "daily_accounting_snapshots"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    local_date: Mapped[str] = mapped_column(String(10))
    timezone: Mapped[str] = mapped_column(String(60))
    revision: Mapped[int]
    as_of: Mapped[datetime]
    payload: Mapped[dict] = mapped_column(JSONB)
    calculation_version: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("session_id", "local_date", "revision"),)
