"""Authenticated Stage 12 cohort checks. All portfolios and observations are SYNTHETIC."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_paper_api as paper_fixtures
from arbiq.analytics.strategy_lab import (
    StrategyLabRequest,
    boundary_summary,
    window_returns,
)
from arbiq.db.models import Journal, PaperSession, PaperTrade, SimulationEvent
from arbiq.portfolio.models import PortfolioCheckpoint
from pydantic import ValidationError
from sqlalchemy import func, select

private_api = paper_fixtures.private_api
URL = "/api/v1/analytics/strategy-lab/compare"
D = Decimal
SAMPLE = datetime(2026, 11, 22, 6, tzinfo=UTC)


def test_request_binds_distinct_independent_portfolios_to_one_utc_window():
    ids = (uuid4(), uuid4())
    end = SAMPLE + timedelta(minutes=5)
    assert StrategyLabRequest(session_ids=ids, start=SAMPLE, end=end).session_ids == ids
    for args in (
        {"session_ids": (ids[0], ids[0]), "start": SAMPLE, "end": end},
        {"session_ids": ids, "start": SAMPLE.replace(tzinfo=None), "end": end},
        {"session_ids": ids, "start": SAMPLE, "end": SAMPLE + timedelta(days=32)},
        {"session_ids": ids, "start": end, "end": SAMPLE},
    ):
        with pytest.raises(ValidationError):
            StrategyLabRequest(**args)


def test_boundary_staleness_and_capital_flow_gate_do_not_emit_fake_returns():
    snapshot = SimpleNamespace(
        as_of=SAMPLE,
        created_at=SAMPLE,
        payload={"equity_cad": "10000", "no_trading_benchmark_cad": "10000"},
    )
    assert boundary_summary(snapshot, SAMPLE)["equity_cad"] == D(10_000)
    old = boundary_summary(snapshot, SAMPLE + timedelta(minutes=11))
    assert old["valuation_status"] == "STALE"
    assert old["equity_cad"] is None and old["benchmark_cad"] is None
    moment = boundary_summary(snapshot, SAMPLE)
    assert (
        window_returns(moment, moment, ledger_reconciled=True, external_flow=D(0))[
            "excess_return_pct"
        ]
        == 0
    )
    for good, flow in ((False, D(0)), (True, D(1)), (True, None)):
        assert all(
            x is None
            for x in window_returns(
                moment, moment, ledger_reconciled=good, external_flow=flow
            ).values()
        )


def create_session(client, label, *, capital="10000", classification="RESEARCH"):
    result = client.post(
        "/api/v1/paper/sessions",
        json={
            "name": "SYNTHETIC " + label,
            "starting_capital_cad": capital,
            "classification": classification,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def add_checkpoint(
    db, session_id, at, equity, benchmark, *, recorded=None, partial=False
):
    db.add(
        PortfolioCheckpoint(
            session_id=session_id,
            as_of=at,
            created_at=recorded if recorded is not None else at,
            calculation_version="SYNTHETIC",
            payload={
                "equity_cad": equity,
                "no_trading_benchmark_cad": benchmark,
                "valuation_status": "PARTIAL" if partial else "COMPLETE_FRESH",
                "capital_utilization_pct": "2.5",
                "reconciliation_delta_cad": "0",
                "unrealized_pnl_cad": "7",
            },
        )
    )


@pytest.mark.asyncio
async def test_same_window_read_only_comparison_uses_causal_marks_and_events(
    private_api, monkeypatch
):
    client, sessions, _ = private_api
    first = UUID(create_session(client, "liquid")["id"])
    second = UUID(create_session(client, "independent")["id"])
    start = datetime.now(UTC) + timedelta(minutes=5)
    end = start + timedelta(minutes=3)

    async def test_clock(request):
        return end + timedelta(minutes=2)

    monkeypatch.setattr("arbiq.analytics.api.now_checked", test_clock)
    async with sessions() as db:
        add_checkpoint(db, first, start - timedelta(seconds=5), "10000", "10000")
        add_checkpoint(db, first, start + timedelta(seconds=60), "10100", "10001")
        add_checkpoint(db, first, start + timedelta(seconds=150), "10010", "10005")
        add_checkpoint(
            db,
            first,
            end - timedelta(seconds=2),
            "20000",
            "10000",
            recorded=end + timedelta(seconds=1),
        )
        add_checkpoint(db, second, start - timedelta(seconds=5), "10000", "10000")
        add_checkpoint(
            db, second, start + timedelta(seconds=150), None, "10000", partial=True
        )
        session = await db.get(PaperSession, first)
        trade = PaperTrade(
            session_id=first,
            strategy_version_id=session.strategy_version_id,
            mode="RESEARCH_SHADOW",
            classification="RESEARCH",
            requested_at=start + timedelta(seconds=1),
            eligible_at=start + timedelta(seconds=1),
            reaction_delay_ms=0,
            status="COMPLETED",
            completed_at=end - timedelta(seconds=2),
            created_at=start + timedelta(seconds=1),
            calculation_version="SYNTHETIC",
            assumptions={},
            state={},
        )
        db.add(trade)
        await db.flush()
        db.add_all(
            [
                SimulationEvent(
                    trade_id=trade.id,
                    event_type="SCHEDULED",
                    reason="SYNTHETIC_SCHEDULED",
                    occurred_at=start + timedelta(seconds=1),
                    created_at=start + timedelta(seconds=1),
                    provenance={"synthetic": True},
                ),
                SimulationEvent(
                    trade_id=trade.id,
                    event_type="COMPLETED",
                    reason="SYNTHETIC_RECORDED_AFTER_CUTOFF",
                    occurred_at=end - timedelta(seconds=2),
                    created_at=end + timedelta(seconds=1),
                    provenance={"synthetic": True},
                ),
            ]
        )
        await db.commit()
        before = await db.scalar(
            select(func.count())
            .select_from(Journal)
            .where(Journal.session_id.in_([first, second]))
        )
    payload = {
        "session_ids": [str(first), str(second)],
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    result = client.post(URL, json=payload)
    assert result.status_code == 200, result.text
    cohort = result.json()
    assert cohort["kind"] == "FORWARD_PAPER_OBSERVED" and cohort["paper_only"]
    assert D(cohort["initial_capital_cad"]) == 10_000
    assert [record["session_id"] for record in cohort["rows"]] == payload["session_ids"]
    first_row, second_row = cohort["rows"]
    assert first_row["profile"] == "BASELINE"
    assert first_row["completed_simulations"] == 0 and first_row["paper_activity"] == {
        "SCHEDULED": 1
    }
    assert first_row["closing"]["equity_cad"] == "10010"
    assert D(first_row["portfolio_return_pct"]) == D("0.1")
    assert D(first_row["benchmark_return_pct"]) == D("0.05")
    assert D(first_row["excess_return_pct"]) == D("0.05")
    assert D(first_row["drawdown_pct"]) == D("0.891089108910891089")
    assert first_row["complete_checkpoint_count"] == 2
    assert first_row["ledger_reconciled"]
    assert (
        second_row["valuation_status"] == "PARTIAL"
        and second_row["closing"]["equity_cad"] is None
    )
    assert (
        second_row["portfolio_return_pct"] is None
        and second_row["drawdown_pct"] is None
    )
    async with sessions() as db:
        after = await db.scalar(
            select(func.count())
            .select_from(Journal)
            .where(Journal.session_id.in_([first, second]))
        )
    assert before == after


@pytest.mark.asyncio
async def test_comparison_rejects_unknown_owner_scope_mismatched_capital_and_validated_portfolios(
    private_api, monkeypatch
):
    client, _, _ = private_api
    first = create_session(client, "one")["id"]
    mismatch = create_session(client, "unequal", capital="50000")["id"]
    live = create_session(client, "validated", classification="VALIDATED")["id"]
    start = datetime.now(UTC) + timedelta(minutes=5)
    end = start + timedelta(minutes=2)

    async def clock(request):
        return end

    monkeypatch.setattr("arbiq.analytics.api.now_checked", clock)
    for other, status in (
        (str(uuid4()), 404),
        (mismatch, 422),
        (live, 422),
        (first, 422),
    ):
        response = client.post(
            URL,
            json={
                "session_ids": [first, other],
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        assert response.status_code == status, response.text
