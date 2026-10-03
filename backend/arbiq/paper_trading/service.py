"""Authenticated portfolio lifecycle helpers; callers own commits."""

from decimal import Decimal, localcontext

from sqlalchemy import func, select

from arbiq.db.models import Asset, Exchange, PaperSession, User
from arbiq.finance.contracts import Contract, PositiveDecimal
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.accounting import fund
from arbiq.portfolio.contracts import decimal_context, stored
from arbiq.settings.strategy import save_strategy


class Allocation(Contract):
    exchange: str
    asset: str = "fiat:CAD"
    quantity: PositiveDecimal
    cad_rate: PositiveDecimal | None = None
    evidence: dict = {}


@decimal_context
async def create_portfolio(
    db, user_id, name, settings, initial_equity, allocations, now, *, observed_funding=False
):
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    count = await db.scalar(
        select(func.count())
        .select_from(PaperControl)
        .join(PaperSession)
        .where(PaperSession.user_id == user_id, PaperControl.status == "ACTIVE")
    )
    if count >= 20:
        raise ValueError("Maximum20active portfolios; pause an existing scenario first")
    version = await save_strategy(db, user_id, settings, now, "Created isolated paper portfolio")
    research = settings.mode == "RESEARCH_SHADOW"
    if allocations is None:
        exchanges = list(
            await db.scalars(
                select(Exchange)
                .where(Exchange.slug.in_(["kraken", "ndax"]))
                .order_by(Exchange.slug)
            )
        )
        if not exchanges:
            raise ValueError("No exchange metadata available for funding")
        with localcontext() as ctx:
            ctx.prec = 78
            per = stored(initial_equity / len(exchanges))
            remainder = initial_equity
            allocations = []
            for i, x in enumerate(exchanges):
                amount = remainder if i == len(exchanges) - 1 else per
                allocations.append(Allocation(exchange=x.slug, quantity=amount))
                remainder -= amount
    if not allocations or len(allocations) > 100:
        raise ValueError("One to100 funded allocations required")
    items = []
    total = Decimal(0)
    for a in allocations:
        exchange = await db.scalar(select(Exchange).where(Exchange.slug == a.exchange))
        asset = await db.scalar(select(Asset).where(Asset.canonical_key == a.asset))
        if not exchange or not asset:
            raise ValueError("Unknown exchange or normalized asset")
        rate = Decimal(1) if asset.canonical_key == "fiat:CAD" else a.cad_rate
        if rate is None:
            raise ValueError("Initial crypto/FX inventory needs a sourced CAD valuation")
        if asset.canonical_key != "fiat:CAD" and not research and not observed_funding:
            raise ValueError("Assumed funding values require an isolated research portfolio")
        value = stored(a.quantity * rate)
        total += value
        items.append((a, exchange, asset, value))
    if total != initial_equity:
        raise ValueError("Initial allocation CAD values must equal starting equity exactly")
    session = PaperSession(
        user_id=user_id,
        strategy_version_id=version.id,
        name=name,
        started_at=now,
        initial_equity_cad=initial_equity,
        benchmark_snapshot={
            "classification": "RESEARCH" if research else "VALIDATED",
            "initial_equity_cad": str(initial_equity),
            "simulation_only": True,
            "holdings": [],
        },
    )
    db.add(session)
    await db.flush()
    control = PaperControl(
        session_id=session.id,
        strategy_version_id=version.id,
        classification="RESEARCH" if research else "VALIDATED",
        mode=settings.mode,
        status="ACTIVE",
        settings=settings.model_dump(mode="json"),
        updated_at=now,
    )
    db.add(control)
    await db.flush()
    for i, (a, exchange, asset, value) in enumerate(items):
        await fund(
            db,
            session.id,
            exchange.id,
            asset.id,
            a.quantity,
            value,
            now,
            f"initial:{session.id}:{i}",
            {
                "classification": control.classification,
                "valuation_source": "OBSERVED" if observed_funding else "OWNER_ALLOCATION",
                "cad_rate": str(value / a.quantity),
                "evidence": a.evidence,
                "no_real_deposit": True,
            },
        )
    return session, control


