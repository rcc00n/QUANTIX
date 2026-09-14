"""PostgreSQL integrity tests. EVERY user, asset and balance here is SYNTHETIC."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from arbiq.db.base import Base
from arbiq.db.models import (
    Asset,
    Exchange,
    Journal,
    LedgerEntry,
    PaperSession,
    StrategyVersion,
    User,
    VirtualAccount,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def synthetic_accounts(db):
    suffix = uuid4().hex
    user = User(username=f"synthetic-{suffix}", password_hash="synthetic-not-for-auth")
    asset = Asset(
        canonical_key=f"synthetic:{suffix}", symbol="SYNTH", kind="fiat", identifiers={}
    )
    exchange = Exchange(slug=f"synthetic-{suffix}", name="Synthetic fixture exchange")
    db.add_all([user, asset, exchange])
    await db.flush()
    strategy = StrategyVersion(
        user_id=user.id,
        version=1,
        settings={"synthetic": True},
        reason="SYNTHETIC integration fixture",
    )
    db.add(strategy)
    await db.flush()
    session = PaperSession(
        user_id=user.id,
        strategy_version_id=strategy.id,
        name="SYNTHETIC",
        started_at=datetime.now(UTC),
        initial_equity_cad=Decimal(1000),
        benchmark_snapshot={"synthetic": True},
    )
    db.add(session)
    await db.flush()
    available = VirtualAccount(
        session_id=session.id,
        exchange_id=exchange.id,
        asset_id=asset.id,
        role="available",
    )
    equity = VirtualAccount(session_id=session.id, asset_id=asset.id, role="equity")
    db.add_all([available, equity])
    await db.flush()
    return session, available, equity


def synthetic_journal(session):
    return Journal(
        session_id=session.id,
        event_type="synthetic_deposit",
        idempotency_key=f"synthetic:{uuid4()}",
        effective_at=datetime.now(UTC),
        provenance={"synthetic": True},
    )


async def test_postgres_roundtrip_ledger_balance_and_immutability(database_url):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            paper, available, equity = await synthetic_accounts(db)
            journal = synthetic_journal(paper)
            db.add(journal)
            await db.flush()
            journal_id, available_id = journal.id, available.id
            amount = Decimal("123456789.123456789123456789")
            db.add_all(
                [
                    LedgerEntry(
                        journal_id=journal_id,
                        account_id=available_id,
                        quantity=amount,
                        cad_value=amount,
                        memo="SYNTHETIC credit",
                    ),
                    LedgerEntry(
                        journal_id=journal.id,
                        account_id=equity.id,
                        quantity=-amount,
                        cad_value=-amount,
                        memo="SYNTHETIC offset",
                    ),
                ]
            )
            await db.commit()
            journal_id, available_id = journal.id, available.id
            entries = (
                await db.scalars(
                    select(LedgerEntry).where(LedgerEntry.journal_id == journal.id)
                )
            ).all()
            assert {entry.quantity for entry in entries} == {amount, -amount}
            assert sum((entry.quantity for entry in entries), Decimal(0)) == 0
            for sql in [
                "UPDATE ledger_entries SET memo = 'forbidden' WHERE journal_id = :id",
                "DELETE FROM ledger_entries WHERE journal_id = :id",
                "TRUNCATE system_events",
            ]:
                with pytest.raises(DBAPIError, match="append-only"):
                    await db.execute(text(sql), {"id": journal_id})
                await db.rollback()
            with pytest.raises(DBAPIError, match="sealed"):
                db.add(
                    LedgerEntry(
                        journal_id=journal_id,
                        account_id=available_id,
                        quantity=Decimal(1),
                        cad_value=Decimal(1),
                        memo="SYNTHETIC",
                    )
                )
                await db.commit()
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "kind", ["unbalanced", "empty", "cross_session", "cross_asset"]
)
async def test_database_rejects_invalid_journals(database_url, kind):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            paper, available, equity = await synthetic_accounts(db)
            if kind == "cross_session":
                _, _, equity = await synthetic_accounts(db)
            if kind == "cross_asset":
                asset = Asset(
                    canonical_key=f"synthetic:{uuid4()}",
                    symbol="OTHER",
                    kind="fiat",
                    identifiers={},
                )
                db.add(asset)
                await db.flush()
                equity = VirtualAccount(
                    session_id=paper.id, asset_id=asset.id, role="equity"
                )
                db.add(equity)
                await db.flush()
            journal = synthetic_journal(paper)
            db.add(journal)
            await db.flush()
            if kind != "empty":
                db.add_all(
                    [
                        LedgerEntry(
                            journal_id=journal.id,
                            account_id=available.id,
                            quantity=Decimal(10),
                            cad_value=Decimal(10),
                            memo="SYNTHETIC",
                        ),
                        LedgerEntry(
                            journal_id=journal.id,
                            account_id=equity.id,
                            quantity=Decimal("-9" if kind == "unbalanced" else "-10"),
                            cad_value=Decimal(-10),
                            memo="SYNTHETIC",
                        ),
                    ]
                )
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
    finally:
        await engine.dispose()




