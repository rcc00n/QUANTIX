"""SYNTHETIC accounting units, prices, users and venues; never live exchange results."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from arbiq.db.models import (
    Asset,
    Exchange,
    PaperSession,
    StrategyVersion,
    User,
)
from arbiq.portfolio.accounting import (
    execute_leg,
    fund,
    owned_reservation,
    position,
    reconcile,
    release_remaining,
    reserve,
    transfer,
)
from arbiq.portfolio.analytics import day_bounds, summarize_trades
from arbiq.portfolio.contracts import (
    AccountingError,
    InsufficientBalance,
    PriceEvidence,
    Reservation,
)
from arbiq.portfolio.valuation import calculate, snapshot
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

D = Decimal
NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)
DEFAULT_CAPITAL = D(10000)


def synthetic_mark(price, quantity=None, observed=NOW):
    return PriceEvidence(
        D(price),
        observed,
        observed,
        False,
        "SYNTHETIC observed price",
        {
            "synthetic": True,
            **({"valued_quantity": str(quantity)} if quantity is not None else {}),
        },
    )


def synthetic_positions():
    venue, asset = uuid4(), uuid4()
    return [
        {
            "exchange_id": venue,
            "asset_id": asset,
            "asset": "BTC",
            "canonical_key": "asset:BTC",
            "kind": "crypto",
            "quantity": D("2"),
            "reserved_quantity": D("0.5"),
            "cost_basis_cad": D("100"),
        }
    ]


def values(positions, prices, **kwargs):
    return calculate(
        positions,
        prices,
        [],
        NOW,
        initial_equity_cad=D("100"),
        external_flow_cad=D("100"),
        realized_trading_cad=D(0),
        realized_fx_cad=D(0),
        fees_cad=D(0),
        expenses_cad=D(0),
        **kwargs,
    )


def test_unrealized_equity_utilization_drawdown_are_distinct():
    rows = synthetic_positions()
    key = (rows[0]["exchange_id"], rows[0]["asset_id"])
    gain = values(rows, {key: synthetic_mark("60")})
    assert D(gain["equity_cad"]) == 120 and D(gain["unrealized_pnl_cad"]) == 20
    assert D(gain["realized_simulated_pnl_cad"]) == 0 and D(gain["capital_utilization_pct"]) == 25
    assert D(gain["reconciliation_delta_cad"]) == 0
    loss = values(rows, {key: synthetic_mark("40")}, previous_peak_cad=D("150"))
    assert D(loss["drawdown_pct"]) == D("46.666666666666666667")
    assert D(loss["net_simulated_pnl_cad"]) == -20


@pytest.mark.parametrize(
    "price",
    [
        None,
        synthetic_mark("50", observed=NOW - timedelta(seconds=61)),
        synthetic_mark("50", observed=NOW + timedelta(seconds=1)),
        PriceEvidence(D(50), None, None),
    ],
)
def test_missing_stale_future_or_untimestamped_marks_never_invent_equity(price):
    rows = synthetic_positions()
    key = (rows[0]["exchange_id"], rows[0]["asset_id"])
    result = values(rows, {key: price} if price else {})
    assert result["equity_cad"] is None and result["unrealized_pnl_cad"] is None
    assert result["valuation_verified"] is False and result["limitations"]


def test_partial_valuation_reports_nonzero_blocking_assets_and_exact_subtotal():
    crypto = {**synthetic_positions()[0], "exchange": "synthetic-venue"}
    missing_eur = {
        **crypto,
        "asset": "EUR",
        "canonical_key": "fiat:EUR",
        "asset_id": uuid4(),
        "quantity": D("5"),
        "reserved_quantity": D(0),
    }
    zero_usdc = {**missing_eur, "asset": "USDC", "asset_id": uuid4(), "quantity": D(0)}
    holdings = [crypto, missing_eur, zero_usdc]
    rate = synthetic_mark("60")
    eur_evidence = PriceEvidence(None, None, None, source="cad_conversion_unavailable")
    result = values(
        holdings,
        {
            (crypto["exchange_id"], crypto["asset_id"]): rate,
            (missing_eur["exchange_id"], missing_eur["asset_id"]): eur_evidence,
        },
    )
    assert result["valuation_status"] == "PARTIAL" and result["equity_cad"] is None
    assert D(result["valued_subtotal_cad"]) == 120
    assert result["unpriced_position_count"] == 1
    blocker = result["unpriced_balances"][0]
    assert blocker["asset"] == "EUR" and blocker["exchange"] == "synthetic-venue"
    assert blocker["detail_reason"] == "cad_conversion_unavailable"
    assert result["balances"][2]["valuation_status"] == "ZERO_POSITION"
    assert result["balances"][2]["valuation_reason"] is None
    stale = values(
        [crypto],
        {
            (crypto["exchange_id"], crypto["asset_id"]): synthetic_mark(
                "60", observed=NOW - timedelta(seconds=61)
            )
        },
    )
    assert stale["valuation_status"] == "STALE" and stale["valued_subtotal_cad"] is None


def test_benchmark_uses_original_units_and_rejects_wrong_depth_size():
    rows = synthetic_positions()
    p = rows[0]
    key = (p["exchange_id"], p["asset_id"])
    holdings = [
        {
            "exchange_id": str(p["exchange_id"]),
            "asset_id": str(p["asset_id"]),
            "quantity": "1",
        }
    ]
    args = {
        "initial_equity_cad": D(100),
        "external_flow_cad": D(100),
        "realized_trading_cad": D(0),
        "realized_fx_cad": D(0),
        "fees_cad": D(0),
        "expenses_cad": D(0),
    }
    result = calculate(rows, {key: synthetic_mark("60", "2")}, holdings, NOW, **args)
    assert result["no_trading_benchmark_cad"] is None
    result = calculate(
        rows,
        {key: synthetic_mark("60", "2")},
        holdings,
        NOW,
        benchmark_prices={key: synthetic_mark("65", "1")},
        **args,
    )
    assert D(result["no_trading_benchmark_cad"]) == 65 and D(result["strategy_excess_cad"]) == 55






async def seed(db, capital=DEFAULT_CAPITAL, name="SYNTHETIC"):
    suffix = uuid4().hex
    user = User(username="synthetic-accounting-" + suffix, password_hash="synthetic-only")
    a = Exchange(slug="synthetic-a-" + suffix, name="SYNTHETIC A")
    b = Exchange(slug="synthetic-b-" + suffix, name="SYNTHETIC B")
    btc = Asset(
        canonical_key="synthetic:BTC:" + suffix,
        symbol="BTC",
        kind="crypto",
        identifiers={"synthetic": True},
    )
    cad = await db.scalar(select(Asset).where(Asset.canonical_key == "fiat:CAD"))
    if cad is None:
        cad = Asset(
            canonical_key="fiat:CAD",
            symbol="CAD",
            kind="fiat",
            identifiers={"synthetic": True},
        )
        db.add(cad)
    db.add_all([user, a, b, btc])
    await db.flush()
    strategy = StrategyVersion(
        user_id=user.id, version=1, settings={"synthetic": True}, reason="SYNTHETIC"
    )
    db.add(strategy)
    await db.flush()
    session = PaperSession(
        user_id=user.id,
        strategy_version_id=strategy.id,
        name=name,
        started_at=NOW,
        initial_equity_cad=capital,
        benchmark_snapshot={},
    )
    db.add(session)
    await db.flush()
    return session, a, b, btc, cad






















