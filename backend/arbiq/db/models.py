"""Persistent domain records. Financial amounts use exact NUMERIC(38,18).

JSONB stores decimal strings, never JSON floating point amounts. Domain writes and
cross-record invariants belong in services, not ORM callbacks. See docs/SCHEMA.md.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arbiq.db.base import Base, Record


class User(Record, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class LoginAttempt(Record, Base):
    __tablename__ = "login_attempts"
    username: Mapped[str] = mapped_column(String(80))
    client_hash: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool]
    __table_args__ = (Index("ix_login_throttle", "client_hash", "created_at"),)


class Exchange(Record, Base):
    __tablename__ = "exchanges"
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(default=False)
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict)
    config_version: Mapped[int] = mapped_column(default=1)


