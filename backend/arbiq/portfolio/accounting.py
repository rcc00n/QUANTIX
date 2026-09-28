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


@decimal_context
async def _post(
    db,
    session_id,
    event,
    key,
    now,
    request,
    evidence,
    entries,
    *,
    source_id=None,
    realized_trading=ZERO,
    realized_fx=ZERO,
    fees=ZERO,
    expenses=ZERO,
    external_flow=ZERO,
    details=None,
):
    utc_timestamp(now)
    nonzero = [(a, stored(q), stored(v), memo) for a, q, v, memo in entries if q != 0]
    totals = defaultdict(lambda: ZERO)
    for account, q, _, _ in nonzero:
        totals[account.asset_id] += q
    if len(nonzero) < 2 or any(totals.values()):
        raise AccountingError("Journal must balance by canonical asset")
    row = Journal(
        session_id=session_id,
        event_type=event,
        idempotency_key=key,
        effective_at=now,
        source_id=source_id,
        provenance={
            "accounting_version": ACCOUNTING_VERSION,
            "request_fingerprint": _fingerprint(request),
            "request": _canonical(request),
            "evidence": safe_json(evidence),
        },
    )
    db.add(row)
    await db.flush()
    for account, q, v, memo in nonzero:
        db.add(
            LedgerEntry(
                journal_id=row.id, account_id=account.id, quantity=q, cad_value=v, memo=memo
            )
        )
    db.add(
        AccountingMovement(
            session_id=session_id,
            journal_id=row.id,
            event_type=event,
            effective_at=now,
            realized_trading_cad=stored(realized_trading),
            realized_fx_cad=stored(realized_fx),
            fees_cad=stored(fees),
            expenses_cad=stored(expenses),
            external_flow_cad=stored(external_flow),
            details=safe_json(details or {}),
        )
    )
    await db.flush()
    return row.id


async def _pair(
    db,
    session_id,
    exchange_id,
    asset_id,
    role,
    quantity,
    cad_value,
    counter_role="equity",
    memo="Paper accounting",
):
    account = await _account(db, session_id, exchange_id, asset_id, role)
    counter = await _account(db, session_id, None, asset_id, counter_role)
    return [
        (account, quantity, cad_value, memo),
        (counter, -quantity, -cad_value if cad_value is not None else None, memo),
    ]


@decimal_context
async def fund(db, session_id, exchange_id, asset_id, quantity, cad_value, now, key, evidence):
    quantity = positive(quantity)
    cad_value = positive(cad_value, "initial CAD value")
    session = await lock_session(db, session_id)
    request = {
        "event": "INITIAL_FUNDING",
        "exchange_id": exchange_id,
        "asset_id": asset_id,
        "quantity": quantity,
        "cad_value": cad_value,
    }
    existing = await _existing(db, session_id, key, request)
    if existing:
        return existing.id
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise AccountingError("Funding asset not found")
    if asset.canonical_key == "fiat:CAD" and quantity != cad_value:
        raise AccountingError("CAD funding must equal its CAD value")
    previous = list(
        await db.scalars(
            select(AccountingMovement).where(AccountingMovement.session_id == session_id)
        )
    )
    if any(x.event_type != "INITIAL_FUNDING" for x in previous):
        raise AccountingError("Initial funding is closed after portfolio activity")
    funded = sum((Decimal(x.details["funding_cad"]) for x in previous), ZERO)
    if funded + cad_value > session.initial_equity_cad:
        raise AccountingError("Initial allocations exceed starting equity")
    p = await position(db, session_id, exchange_id, asset_id)
    with localcontext() as ctx:
        ctx.prec = 78
        p.quantity = stored(p.quantity + quantity)
        p.cost_basis_cad = (
            stored((p.cost_basis_cad or ZERO) + cad_value) if p.cost_basis_cad is not None else None
        )
        p.updated_at = now
        entries = await _pair(
            db,
            session_id,
            exchange_id,
            asset_id,
            "available",
            quantity,
            cad_value,
            memo="Initial virtual funding; CAD basis charged",
        )
        journal = await _post(
            db,
            session_id,
            "INITIAL_FUNDING",
            key,
            now,
            request,
            evidence,
            entries,
            external_flow=cad_value,
            details={
                "funding_cad": str(cad_value),
                "quantity": str(quantity),
                "asset_id": str(asset_id),
                "exchange_id": str(exchange_id),
            },
        )
        statement = insert(Asset).values(
            canonical_key="fiat:CAD",
            symbol="CAD",
            kind="fiat",
            identifiers={"identity_verified": True},
        )
        await db.execute(statement.on_conflict_do_nothing(index_elements=[Asset.canonical_key]))
        cad_id = await db.scalar(select(Asset.id).where(Asset.canonical_key == "fiat:CAD"))
        fx = FXQuote(
            base_asset_id=asset_id,
            quote_asset_id=cad_id,
            rate=stored(cad_value / quantity),
            observed_at=now,
            source="Initial paper inventory valuation",
            provenance={
                "kind": "INITIAL_VALUATION_NOT_EXECUTION",
                "evidence": safe_json(evidence),
                "cad_value": str(cad_value),
            },
        )
        db.add(fx)
        await db.flush()
        account = await _account(db, session_id, exchange_id, asset_id, "available")
        db.add(
            VirtualDeposit(
                account_id=account.id,
                journal_id=journal,
                fx_quote_id=fx.id,
                quantity=quantity,
                cad_equivalent=cad_value,
                is_initial=True,
            )
        )
        snapshot = dict(session.benchmark_snapshot or {})
        holdings = list(snapshot.get("holdings", []))
        holdings.append(
            {
                "exchange_id": str(exchange_id),
                "asset_id": str(asset_id),
                "quantity": str(quantity),
                "initial_cad_value": str(cad_value),
                "source_journal_id": str(journal),
            }
        )
        session.benchmark_snapshot = {
            **snapshot,
            "holdings": holdings,
            "convention": (
                "Original funded units held unchanged; "
                "same future valuation inputs as actual portfolio"
            ),
        }
        await db.flush()
    return journal


@decimal_context
def _combine(requirements):
    result = defaultdict(lambda: ZERO)
    for r in requirements:
        positive(r.quantity)
        result[(r.exchange_id, r.asset_id)] += r.quantity
    return [
        (key, stored(value))
        for key, value in sorted(result.items(), key=lambda x: (str(x[0][0]), str(x[0][1])))
    ]


@decimal_context
async def reserve(db, session_id, requirements, now, key, evidence):
    await lock_session(db, session_id)
    combined = _combine(requirements)
    request = {
        "event": "RESERVATION",
        "requirements": [
            {"exchange_id": x, "asset_id": a, "quantity": q} for (x, a), q in combined
        ],
    }
    existing = await _existing(db, session_id, key, request)
    if existing:
        return existing.id
    if not combined:
        raise AccountingError("Empty reservation")
    positions = []
    for (exchange_id, asset_id), amount in combined:
        p = await position(db, session_id, exchange_id, asset_id)
        if p.quantity - p.reserved_quantity < amount:
            raise InsufficientBalance("Insufficient available virtual inventory")
        positions.append((p, amount))
    entries = []
    with localcontext() as ctx:
        ctx.prec = 78
        for p, amount in positions:
            value = (
                stored(p.cost_basis_cad * amount / p.quantity)
                if p.cost_basis_cad is not None
                else None
            )
            avail = await _account(db, session_id, p.exchange_id, p.asset_id, "available")
            reserved = await _account(db, session_id, p.exchange_id, p.asset_id, "reserved")
            entries.extend(
                [
                    (
                        avail,
                        -amount,
                        -value if value is not None else None,
                        "Reserve for one simulation",
                    ),
                    (reserved, amount, value, "Reserve for one simulation"),
                ]
            )
            p.reserved_quantity = stored(p.reserved_quantity + amount)
            p.updated_at = now
    return await _post(
        db,
        session_id,
        "RESERVATION",
        key,
        now,
        request,
        evidence,
        entries,
        details={"reservation_key": key},
    )


@decimal_context
async def owned_reservation(db, session_id, reservation_key):
    journal = await db.scalar(
        select(Journal).where(
            Journal.session_id == session_id,
            Journal.idempotency_key == reservation_key,
            Journal.event_type == "RESERVATION",
        )
    )
    if journal is None:
        raise AccountingError("Reservation ownership not found")
    original = journal.provenance["request"]["requirements"]
    remaining = {
        (UUID(r["exchange_id"]), UUID(r["asset_id"])): Decimal(r["quantity"]) for r in original
    }
    movements = list(
        await db.scalars(
            select(AccountingMovement).where(
                AccountingMovement.session_id == session_id,
                AccountingMovement.details["reservation_key"].as_string() == reservation_key,
            )
        )
    )
    for movement in movements:
        for r in movement.details.get("reservation_consumption", []):
            key = (UUID(r["exchange_id"]), UUID(r["asset_id"]))
            remaining[key] = remaining.get(key, ZERO) - Decimal(r["quantity"])
    if any(v < 0 for v in remaining.values()):
        raise AccountingError("Reservation ownership overconsumed")
    return remaining


@decimal_context
async def release_remaining(db, session_id, reservation_key, now, key, evidence):
    await lock_session(db, session_id)
    # Repeated releases use the original committed request, independently of the
    # remaining quantity (which is zero after the first successful release).
    existing = await db.scalar(select(Journal).where(Journal.idempotency_key == key))
    if existing:
        if (
            existing.session_id != session_id
            or existing.provenance.get("request", {}).get("reservation_key") != reservation_key
        ):
            raise AccountingError("Release idempotency key mismatch")
        return existing.id
    remaining = await owned_reservation(db, session_id, reservation_key)
    active = [(k, q) for k, q in remaining.items() if q > 0]
    if not active:
        return None
    entries = []
    consumption = []
    with localcontext() as ctx:
        ctx.prec = 78
        for (exchange_id, asset_id), amount in active:
            p = await position(db, session_id, exchange_id, asset_id)
            if p.reserved_quantity < amount:
                raise AccountingError("Reserved ledger is inconsistent")
            value = (
                stored(p.cost_basis_cad * amount / p.quantity)
                if p.cost_basis_cad is not None
                else None
            )
            reserved = await _account(db, session_id, exchange_id, asset_id, "reserved")
            avail = await _account(db, session_id, exchange_id, asset_id, "available")
            entries.extend(
                [
                    (
                        reserved,
                        -amount,
                        -value if value is not None else None,
                        "Release unused simulation reservation",
                    ),
                    (avail, amount, value, "Release unused simulation reservation"),
                ]
            )
            p.reserved_quantity = stored(p.reserved_quantity - amount)
            p.updated_at = now
            consumption.append(
                {
                    "exchange_id": str(exchange_id),
                    "asset_id": str(asset_id),
                    "quantity": str(amount),
                }
            )
    return await _post(
        db,
        session_id,
        "RESERVATION_RELEASE",
        key,
        now,
        {"reservation_key": reservation_key},
        evidence,
        entries,
        details={"reservation_key": reservation_key, "reservation_consumption": consumption},
    )


def _remove_basis(p, amount):
    if amount > p.quantity:
        raise InsufficientBalance("Cannot dispose of nonexistent virtual inventory")
    with localcontext() as ctx:
        ctx.prec = 78
        removed = (
            stored(p.cost_basis_cad * amount / p.quantity)
            if p.cost_basis_cad is not None and p.quantity
            else None
        )
        p.quantity = stored(p.quantity - amount)
        p.cost_basis_cad = (
            stored(p.cost_basis_cad - removed)
            if p.cost_basis_cad is not None and removed is not None
            else None
        )
        if p.quantity == 0:
            p.cost_basis_cad = ZERO
    return removed


def _add_basis(p, amount, cad_value):
    p.quantity = stored(p.quantity + amount)
    p.cost_basis_cad = (
        stored(p.cost_basis_cad + cad_value)
        if p.cost_basis_cad is not None and cad_value is not None
        else None
    )


@decimal_context
async def execute_leg(
    db,
    session_id,
    exchange_id,
    base_asset_id,
    quote_asset_id,
    side,
    quantity,
    quote_notional,
    fee_quantity,
    fee_currency,
    base_cad_per_unit,
    quote_cad_per_unit,
    now,
    key,
    source_id,
    evidence,
    *,
    from_reserved=True,
    reservation_key=None,
):
    quantity = positive(quantity)
    quote_notional = positive(quote_notional, "quote notional")
    fee_quantity = positive(fee_quantity, "fee", zero=True)
    if (
        side not in {"buy", "sell"}
        or fee_currency not in {"base", "quote"}
        or base_asset_id == quote_asset_id
    ):
        raise AccountingError("Invalid paper execution identity")
    for rate in (base_cad_per_unit, quote_cad_per_unit):
        if rate is not None and (
            not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0
        ):
            raise AccountingError("Invalid CAD valuation evidence")
    if from_reserved and not reservation_key:
        raise AccountingError("Execution requires its owned reservation")
    await lock_session(db, session_id)
    request = {
        "event": "PAPER_LEG",
        "exchange_id": exchange_id,
        "base_asset_id": base_asset_id,
        "quote_asset_id": quote_asset_id,
        "side": side,
        "quantity": quantity,
        "quote_notional": quote_notional,
        "fee_quantity": fee_quantity,
        "fee_currency": fee_currency,
        "base_cad_per_unit": base_cad_per_unit,
        "quote_cad_per_unit": quote_cad_per_unit,
        "from_reserved": from_reserved,
        "reservation_key": reservation_key,
        "source_id": source_id,
    }
    existing = await _existing(db, session_id, key, request)
    if existing:
        movement = await db.scalar(
            select(AccountingMovement).where(AccountingMovement.journal_id == existing.id)
        )
        d = movement.details
        return LegAccounting(
            existing.id,
            True,
            Decimal(d["base_change"]),
            Decimal(d["quote_change"]),
            Decimal(d["cost_basis_removed_cad"])
            if d.get("cost_basis_removed_cad") is not None
            else None,
            movement.realized_trading_cad,
            movement.realized_fx_cad,
            movement.fees_cad,
        )
    base = await position(db, session_id, exchange_id, base_asset_id)
    quote = await position(db, session_id, exchange_id, quote_asset_id)
    role = "reserved" if from_reserved else "available"
    owned = await owned_reservation(db, session_id, reservation_key) if from_reserved else {}
    with localcontext() as ctx:
        ctx.prec = 78
        base_fee = fee_quantity if fee_currency == "base" else ZERO
        quote_fee = fee_quantity if fee_currency == "quote" else ZERO
        if side == "buy":
            base_change = quantity - base_fee
            quote_change = -(quote_notional + quote_fee)
            spending = quote
            spend_amount = -quote_change
            if base_change <= 0:
                raise AccountingError("Fees consume acquired virtual asset")
        else:
            base_change = -(quantity + base_fee)
            quote_change = quote_notional - quote_fee
            spending = base
            spend_amount = -base_change
            if quote_change <= 0:
                raise AccountingError("Fees consume virtual sale proceeds")
        capacity = (
            spending.reserved_quantity
            if from_reserved
            else spending.quantity - spending.reserved_quantity
        )
        if capacity < spend_amount:
            raise InsufficientBalance("Insufficient virtual funds at execution")
        if from_reserved and owned.get((exchange_id, spending.asset_id), ZERO) < spend_amount:
            raise InsufficientBalance("Simulation cannot spend another reservation")
        removed = _remove_basis(spending, spend_amount)
        if from_reserved:
            spending.reserved_quantity = stored(spending.reserved_quantity - spend_amount)
        q_value = (
            stored(quote_notional * quote_cad_per_unit) if quote_cad_per_unit is not None else None
        )
        quote_net_value = (
            stored(quote_change * quote_cad_per_unit) if quote_cad_per_unit is not None else None
        )
        if side == "buy":
            acquisition = (
                stored(-quote_change * quote_cad_per_unit)
                if quote_cad_per_unit is not None
                else None
            )
            _add_basis(base, base_change, acquisition)
            realized_fx = (
                stored(acquisition - removed)
                if acquisition is not None and removed is not None
                else None
            )
            realized_trading = ZERO
        else:
            _add_basis(quote, quote_change, quote_net_value)
            realized_trading = (
                stored(quote_net_value - removed)
                if quote_net_value is not None and removed is not None
                else None
            )
            realized_fx = ZERO
        rate = base_cad_per_unit if fee_currency == "base" else quote_cad_per_unit
        fee_cad = (
            stored(fee_quantity * rate)
            if rate is not None
            else (ZERO if fee_quantity == 0 else None)
        )
        for p in (base, quote):
            p.updated_at = now
        entries = []
        if side == "buy":
            bv = stored(quantity * base_cad_per_unit) if base_cad_per_unit is not None else None
            entries += await _pair(
                db,
                session_id,
                exchange_id,
                base_asset_id,
                "available",
                quantity,
                bv,
                memo="Paper buy observed depth",
            )
            entries += await _pair(
                db,
                session_id,
                exchange_id,
                quote_asset_id,
                role,
                -quote_notional,
                -q_value if q_value is not None else None,
                memo="Paper buy quote principal",
            )
        else:
            bv = stored(quantity * base_cad_per_unit) if base_cad_per_unit is not None else None
            entries += await _pair(
                db,
                session_id,
                exchange_id,
                base_asset_id,
                role,
                -quantity,
                -bv if bv is not None else None,
                memo="Paper sell observed depth",
            )
            entries += await _pair(
                db,
                session_id,
                exchange_id,
                quote_asset_id,
                "available",
                quote_notional,
                q_value,
                memo="Paper sell quote principal",
            )
        if fee_quantity:
            fee_asset = base_asset_id if fee_currency == "base" else quote_asset_id
            fee_role = (
                role
                if (
                    (side == "buy" and fee_currency == "quote")
                    or (side == "sell" and fee_currency == "base")
                )
                else "available"
            )
            entries += await _pair(
                db,
                session_id,
                exchange_id,
                fee_asset,
                fee_role,
                -fee_quantity,
                -fee_cad if fee_cad is not None else None,
                counter_role="expense",
                memo="Explicit simulated taker fee",
            )
        consumption = (
            [
                {
                    "exchange_id": str(exchange_id),
                    "asset_id": str(spending.asset_id),
                    "quantity": str(spend_amount),
                }
            ]
            if from_reserved
            else []
        )
        details = {
            "base_change": str(base_change),
            "quote_change": str(quote_change),
            "cost_basis_removed_cad": str(removed) if removed is not None else None,
            "side": side,
            "base_asset_id": str(base_asset_id),
            "quote_asset_id": str(quote_asset_id),
            "exchange_id": str(exchange_id),
            "fee_currency": fee_currency,
            "fee_quantity": str(fee_quantity),
            "quote_notional": str(quote_notional),
            "quantity": str(quantity),
            "reservation_key": reservation_key,
            "reservation_consumption": consumption,
            "cost_convention": (
                "Fees included in cashflows/basis; disclosed fees are never deducted twice"
            ),
        }
        journal = await _post(
            db,
            session_id,
            "PAPER_LEG",
            key,
            now,
            request,
            evidence,
            entries,
            source_id=source_id,
            realized_trading=realized_trading,
            realized_fx=realized_fx,
            fees=fee_cad,
            details=details,
        )
    return LegAccounting(
        journal,
        False,
        stored(base_change),
        stored(quote_change),
        removed,
        realized_trading,
        realized_fx,
        fee_cad,
    )


@decimal_context
async def transfer(
    db,
    session_id,
    from_exchange_id,
    to_exchange_id,
    asset_id,
    quantity,
    fee_quantity,
    cad_per_unit,
    requested_at,
    settled_at,
    key,
    evidence,
    *,
    reservation_key=None,
):
    """Settle explicitly configured virtual transfer; no instantaneous network claim.

    quantity is the amount credited at destination; fee_quantity is additional
    source inventory consumed. Root's scheduler must wait for settlement first.
    """
    from datetime import timedelta

    from arbiq.db.models import InventoryTransfer

    quantity = positive(quantity)
    fee_quantity = positive(fee_quantity, "transfer fee", zero=True)
    if from_exchange_id == to_exchange_id:
        raise AccountingError("Transfer venues must differ")
    utc_timestamp(requested_at)
    utc_timestamp(settled_at)
    delay = evidence.get("settlement_delay_seconds")
    if (
        isinstance(delay, bool)
        or not isinstance(delay, int)
        or delay < 0
        or settled_at < requested_at + timedelta(seconds=delay)
    ):
        raise AccountingError("Transfer settlement delay has not elapsed")
    if not evidence.get("network_compatible_verified") and not (
        evidence.get("research_only") and evidence.get("network_compatible_assumed")
    ):
        raise AccountingError("Transfer network compatibility is unverified")
    if cad_per_unit is not None and (
        not isinstance(cad_per_unit, Decimal) or not cad_per_unit.is_finite() or cad_per_unit <= 0
    ):
        raise AccountingError("Invalid transfer CAD rate")
    await lock_session(db, session_id)
    request = {
        "event": "INVENTORY_TRANSFER",
        "from_exchange_id": from_exchange_id,
        "to_exchange_id": to_exchange_id,
        "asset_id": asset_id,
        "quantity": quantity,
        "fee_quantity": fee_quantity,
        "cad_per_unit": cad_per_unit,
        "requested_at": requested_at.isoformat(),
        "settled_at": settled_at.isoformat(),
        "reservation_key": reservation_key,
    }
    existing = await _existing(db, session_id, key, request)
    if existing:
        return existing.id
    source = await position(db, session_id, from_exchange_id, asset_id)
    target = await position(db, session_id, to_exchange_id, asset_id)
    with localcontext() as ctx:
        ctx.prec = 78
        debit = quantity + fee_quantity
        available = (
            source.reserved_quantity
            if reservation_key
            else source.quantity - source.reserved_quantity
        )
        if available < debit:
            raise InsufficientBalance("Transfer exceeds funded virtual inventory")
        if reservation_key:
            owned = await owned_reservation(db, session_id, reservation_key)
            if owned.get((from_exchange_id, asset_id), ZERO) < debit:
                raise InsufficientBalance("Transfer cannot spend another reservation")
        removed = _remove_basis(source, debit)
        target_basis = stored(removed * quantity / debit) if removed is not None else None
        fee_basis = stored(removed - target_basis) if removed is not None else None
        if reservation_key:
            source.reserved_quantity = stored(source.reserved_quantity - debit)
        _add_basis(target, quantity, target_basis)
        source.updated_at = settled_at
        target.updated_at = settled_at
        role = "reserved" if reservation_key else "available"
        from_account = await _account(db, session_id, from_exchange_id, asset_id, role)
        to_account = await _account(db, session_id, to_exchange_id, asset_id, "available")
        amount_value = stored(quantity * cad_per_unit) if cad_per_unit is not None else None
        fee_value = (
            stored(fee_quantity * cad_per_unit)
            if cad_per_unit is not None
            else (ZERO if not fee_quantity else None)
        )
        entries = [
            (
                from_account,
                -quantity,
                -amount_value if amount_value is not None else None,
                "Settle virtual transfer source",
            ),
            (to_account, quantity, amount_value, "Settle virtual transfer destination"),
        ]
        if fee_quantity:
            entries += await _pair(
                db,
                session_id,
                from_exchange_id,
                asset_id,
                role,
                -fee_quantity,
                -fee_value if fee_value is not None else None,
                counter_role="expense",
                memo="Virtual withdrawal/network cost",
            )
        details = {
            "reservation_key": reservation_key,
            "reservation_consumption": [
                {
                    "exchange_id": str(from_exchange_id),
                    "asset_id": str(asset_id),
                    "quantity": str(debit),
                }
            ]
            if reservation_key
            else [],
            "from_exchange_id": str(from_exchange_id),
            "to_exchange_id": str(to_exchange_id),
            "asset_id": str(asset_id),
            "quantity": str(quantity),
            "fee_quantity": str(fee_quantity),
            "fee_basis_cad": str(fee_basis) if fee_basis is not None else None,
            "expense_embedded_in_realized": True,
        }
        journal = await _post(
            db,
            session_id,
            "INVENTORY_TRANSFER",
            key,
            settled_at,
            request,
            evidence,
            entries,
            realized_trading=-fee_basis if fee_basis is not None else None,
            expenses=fee_value,
            details=details,
        )
        db.add(
            InventoryTransfer(
                session_id=session_id,
                from_account_id=from_account.id,
                to_account_id=to_account.id,
                journal_id=journal,
                quantity=quantity,
                expense_quantity=fee_quantity,
                expense_asset_id=asset_id,
                expense_cad=fee_value,
                requested_at=requested_at,
                settled_at=settled_at,
                status="settled",
                assumptions=safe_json(evidence),
            )
        )
        await db.flush()
    return journal
