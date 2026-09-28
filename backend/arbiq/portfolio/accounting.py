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


