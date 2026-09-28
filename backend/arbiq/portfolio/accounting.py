"""Transactional paper-only accounting. Callers own the database transaction.

Every operation locks its paper session before checking an idempotency key or
inventory. Immutable per-asset balanced journals remain the durable source of truth.
No method commits, sends an exchange order, or creates an unpriced initial deposit.
"""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from arbiq.db.models import (
    Asset,
    FXQuote,
    Journal,
    LedgerEntry,
    PaperSession,
    VirtualAccount,
    VirtualDeposit,
)
from arbiq.finance.contracts import utc_timestamp
from arbiq.market_data.quality import safe_json
from arbiq.portfolio.contracts import (
    ACCOUNTING_VERSION,
    AccountingError,
    InsufficientBalance,
    LegAccounting,
    decimal_context,
    positive,
    stored,
)
from arbiq.portfolio.models import AccountingMovement, PortfolioPosition

ZERO = Decimal(0)


def _canonical(value):
    if isinstance(value, Decimal):
        with localcontext() as ctx:
            ctx.prec = 78
            return format(value.normalize(), "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


def _fingerprint(request):
    return hashlib.sha256(
        json.dumps(_canonical(request), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def lock_session(db, session_id: UUID) -> PaperSession:
    session = await db.scalar(
        select(PaperSession).where(PaperSession.id == session_id).with_for_update()
    )
    if session is None:
        raise AccountingError("Paper session not found")
    return session


async def _existing(db, session_id, key, request):
    if not isinstance(key, str) or not 1 <= len(key) <= 180:
        raise AccountingError("Invalid idempotency key")
    row = await db.scalar(select(Journal).where(Journal.idempotency_key == key))
    if row and (
        row.session_id != session_id
        or row.provenance.get("request_fingerprint") != _fingerprint(request)
    ):
        raise AccountingError("Idempotency key reused for different accounting inputs")
    return row


async def _account(db, session_id, exchange_id, asset_id, role):
    row = await db.scalar(
        select(VirtualAccount).where(
            VirtualAccount.session_id == session_id,
            VirtualAccount.exchange_id.is_(None)
            if exchange_id is None
            else VirtualAccount.exchange_id == exchange_id,
            VirtualAccount.asset_id == asset_id,
            VirtualAccount.role == role,
        )
    )
    if row is None:
        row = VirtualAccount(
            session_id=session_id, exchange_id=exchange_id, asset_id=asset_id, role=role
        )
        db.add(row)
        await db.flush()
    return row


async def _ledger_quantities(db, session_id, exchange_id, asset_id):
    rows = (
        await db.execute(
            select(VirtualAccount.role, func.coalesce(func.sum(LedgerEntry.quantity), 0))
            .outerjoin(LedgerEntry, LedgerEntry.account_id == VirtualAccount.id)
            .where(
                VirtualAccount.session_id == session_id,
                VirtualAccount.exchange_id == exchange_id,
                VirtualAccount.asset_id == asset_id,
                VirtualAccount.role.in_(["available", "reserved"]),
            )
            .group_by(VirtualAccount.role)
        )
    ).all()
    amounts = dict(rows)
    return amounts.get("available", ZERO), amounts.get("reserved", ZERO)


@decimal_context
async def position(db, session_id, exchange_id, asset_id):
    row = await db.scalar(
        select(PortfolioPosition)
        .where(
            PortfolioPosition.session_id == session_id,
            PortfolioPosition.exchange_id == exchange_id,
            PortfolioPosition.asset_id == asset_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    available, reserved = await _ledger_quantities(db, session_id, exchange_id, asset_id)
    if row is None:
        row = PortfolioPosition(
            session_id=session_id,
            exchange_id=exchange_id,
            asset_id=asset_id,
            quantity=available + reserved,
            reserved_quantity=reserved,
            cost_basis_cad=ZERO if available + reserved == 0 else None,
            updated_at=datetime.now(UTC),
        )
        db.add(row)
        await db.flush()
    elif row.quantity != available + reserved or row.reserved_quantity != reserved:
        raise AccountingError("Position cache does not reconcile to immutable ledger")
    return row


@decimal_context
async def reconcile(db, session_id):
    rows = list(
        await db.scalars(
            select(PortfolioPosition).where(PortfolioPosition.session_id == session_id)
        )
    )
    for row in rows:
        available, reserved = await _ledger_quantities(
            db, session_id, row.exchange_id, row.asset_id
        )
        if (
            row.quantity != available + reserved
            or row.reserved_quantity != reserved
            or available < 0
            or reserved < 0
        ):
            raise AccountingError("Portfolio ledger reconciliation failed")
    return True


