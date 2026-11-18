"""Synthetic common opening publication, archival and exact accounting checks."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_paper_api as paper_fixtures
from arbiq.db.models import Asset, PaperSession, PaperTrade, StrategyVersion
from arbiq.opportunities.contracts import identity_cad
from arbiq.paper_trading import cohort
from arbiq.paper_trading.cohort_market import SelectedMarket, opening_demands
from arbiq.paper_trading.coordinator import scheduled_projection
from arbiq.paper_trading.execution import enforce_cohort_buy_cap
from arbiq.paper_trading.models import PaperControl, StrategyHead
from arbiq.portfolio.accounting import reconcile
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint
from arbiq.settings.models import FundingExperiment
from sqlalchemy import select
from test_paper_execution import book

private_api = paper_fixtures.private_api
BASE = "/api/v1/paper"


def test_scheduling_prefers_qualified_target_and_honors_the_hard_cap(monkeypatch):
    """Only the target preference is synthetic here; economic gates are real."""
    budgets = []
    target_qualifies = True

    def projection(buy, sell, *, budget_cad, **kwargs):
        budgets.append(budget_cad)
        target = len(budgets) == 1
        return SimpleNamespace(
            best=SimpleNamespace(
                acquisition_cost_cad=budget_cad,
                net_profit_cad=Decimal(6 if not target or target_qualifies else 0),
            )
        )

    monkeypatch.setattr("arbiq.paper_trading.coordinator.optimize_route", projection)
    at = datetime.now(UTC)
    strategy = cohort.cohort_strategy()
    assert scheduled_projection(
        None, None, strategy, at
    ).acquisition_cost_cad < Decimal(250)
    assert len(budgets) == 1
    budgets.clear()
    target_qualifies = False
    chosen = scheduled_projection(None, None, strategy, at)
    assert Decimal(250) < chosen.acquisition_cost_cad <= Decimal(500)
    assert len(budgets) == 2
    assert budgets[0] < Decimal(250) < budgets[-1] < Decimal(500)


def test_exact_fee_inclusive_buy_limit_fails_closed_at_execution():
    at = datetime(2026, 11, 22, 6, tzinfo=UTC)
    strategy = cohort.cohort_strategy()
    enforce_cohort_buy_cap(Decimal(500), identity_cad(), at, strategy)
    with pytest.raises(ValueError, match="MAXIMUM_BUY_CAPITAL"):
        enforce_cohort_buy_cap(
            Decimal("500.000000000000000001"), identity_cad(), at, strategy
        )
    with pytest.raises(ValueError, match="BUY_CAP_CAD_EVIDENCE_UNAVAILABLE"):
        enforce_cohort_buy_cap(Decimal(490), None, at, strategy)


def test_opening_uses_losing_depth_without_synthesizing_an_anomaly():
    at = datetime(2026, 11, 22, 6, tzinfo=UTC)
    base = SimpleNamespace(canonical_key="asset:BTC", kind="crypto", symbol="BTC")
    cad = SimpleNamespace(
        id=uuid4(), canonical_key="fiat:CAD", kind="fiat", symbol="CAD"
    )
    markets, books = [], []
    for venue in ("SYNTHETIC_FIRST", "SYNTHETIC_SECOND"):
        market = SimpleNamespace(
            id=uuid4(),
            native_symbol="BTC/CAD",
            amount_step=Decimal("0.01"),
            price_tick=Decimal("0.01"),
            min_notional=Decimal(1),
            metadata_json={"minimum_quantity": "0.01"},
        )
        markets.append(SelectedMarket(market, SimpleNamespace(slug=venue), base, cad))
        books.append(
            book(market.id, venue, at - timedelta(seconds=1)).model_dump(mode="json")
        )
    strategy = cohort.cohort_strategy()
    demands, evidence, _, errors = opening_demands(
        markets, books, at, strategy, timedelta(seconds=10)
    )
    assert not errors
    assert len(evidence) == 2
    assert any(demand.acquisition_cad >= Decimal(250) for demand in demands)
    assert all(
        demand.net_profit_cad < 0 and demand.evaluation_id is None for demand in demands
    )
    assert all(
        demand.buy.quantity > 0 and demand.sell.quantity > 0 for demand in demands
    )
    assert all(
        item["source"] == "LIVE_COLLECTOR_DEPTH_COMMON_OPENING"
        for item in evidence.values()
    )
    assert not any("resolved_reference_anomaly" in item for item in evidence.values())
    books[1]["exchange_timestamp"] = None
    demands, _, _, errors = opening_demands(
        markets, books, at, strategy, timedelta(seconds=10)
    )
    assert not demands
    assert errors == {"SOURCE_TIMESTAMP_MISSING": 1}


async def opening_fixture(dbs, at, asset_key):
    second = f"synthetic-cohort:{uuid4().hex}"
    async with dbs() as db:
        db.add(Asset(canonical_key=second, symbol="SYN2", kind="crypto"))
        await db.commit()
    evidence = {
        "source": "SYNTHETIC COMMON OPENING",
        "observed_at": at.isoformat(),
        "received_at": at.isoformat(),
    }
    strategy = cohort.cohort_strategy().model_copy(
        update={
            "allowed_assets": (asset_key, second),
            "allowed_exchanges": ("kraken", "ndax"),
        }
    )
    rows = [
        ("kraken", "fiat:CAD", "5000", "1", "5000"),
        ("ndax", asset_key, "10", "250", "2500"),
        ("ndax", second, "5", "500", "2500"),
    ]
    return {
        "cohort_plan_version": cohort.VERSION,
        "creatable": True,
        "feasible": True,
        "expires_at": (at + timedelta(seconds=30)).isoformat(),
        "unallocated_cad": "0",
        "allocations": [
            {
                "exchange": venue,
                "asset": asset,
                "quantity": qty,
                "cad_rate": rate,
                "cad_value": value,
                "evidence": evidence,
            }
            for venue, asset, qty, rate, value in rows
        ],
        "allocation_summary": {"reserve_cad": "5000"},
        "coverage_by_size": [
            {
                "trade_amount_cad": "250",
                "funded_alternative_routes": 2,
                "simultaneous_funded_routes": 2,
            }
        ],
        "funded_routes": [{"base_asset": name} for name in (asset_key, second)],
        "funded_base_assets": [asset_key, second],
        "proposed_strategy": strategy.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_common_publication_is_atomic_reconciled_and_idempotent(
    private_api, monkeypatch
):
    client, dbs, asset = private_api
    path = BASE + "/funding"
    old = client.post(
        BASE + "/sessions", json={"name": "SYNTHETIC replace atomically"}
    ).json()
    old_id = UUID(old["id"])
    at = datetime.now(UTC)
    precise = await opening_fixture(dbs, at, asset)

    async def prepare(request, db):
        return precise

    async def clock(request):
        return at

    async def check(request, proposal, strategy):
        return at

    monkeypatch.setattr(cohort, "plan_payload", prepare)
    monkeypatch.setattr(cohort, "now_checked", clock)
    monkeypatch.setattr(cohort, "validate_live_proposal", check)
    before = client.post(path + "/cohort-plan", json={})
    assert before.status_code == 200
    assert before.json()["id"] is None
    planned = client.post(path + "/cohort-plan", json={"preview_only": False})
    assert planned.status_code == 200, planned.text
    body = {
        "proposal_id": planned.json()["id"],
        "experiment_id": str(uuid4()),
        "archive_session_ids": [old["id"]],
    }
    async with dbs() as db:
        row = await db.get(PaperSession, old_id)
        owner = row.user_id
        previous_head = (await db.get(StrategyHead, owner)).strategy_version_id

    # Force the last leg of the transaction to fail and verify both the
    # archival and every earlier per-session journal rolled back.
    actual_snapshot = cohort.snapshot
    checked = 0

    async def fail_late(*args, **kwargs):
        nonlocal checked
        checked += 1
        result = await actual_snapshot(*args, **kwargs)
        if checked == 4:
            raise cohort.AccountingError("SYNTHETIC opening mismatch")
        return result

    monkeypatch.setattr(cohort, "snapshot", fail_late)
    assert client.post(path + "/cohorts", json=body).status_code == 409
    assert client.get(BASE + "/sessions?scope=archived").json()["items"] == []
    assert [s["id"] for s in client.get(BASE + "/sessions").json()["items"]] == [
        old["id"]
    ]
    assert client.get(BASE + "/sessions").json()["items"][0]["status"] == "ACTIVE"
    async with dbs() as db:
        assert (
            list(
                await db.scalars(
                    select(FundingExperiment).where(FundingExperiment.user_id == owner)
                )
            )
            == []
        )

    monkeypatch.setattr(cohort, "snapshot", actual_snapshot)
    created = client.post(path + "/cohorts", json=body)
    assert created.status_code == 201, created.text
    data = created.json()
    assert not data["reused"]
    assert data["started_at"] == at.isoformat()
    assert {s["name"] for s in data["sessions"]} == set(cohort.PROFILES.values())
    assert {s["lab_cohort"]["id"] for s in data["sessions"]} == {body["experiment_id"]}
    expected_ids = {s["id"] for s in data["sessions"]}
    retry = client.post(path + "/cohorts", json=body).json()
    assert retry["reused"] and {s["id"] for s in retry["sessions"]} == expected_ids
    assert (
        client.post(
            path + "/cohorts", json={**body, "archive_session_ids": []}
        ).status_code
        == 409
    )
    assert {
        s["id"] for s in client.get(BASE + "/sessions").json()["items"]
    } == expected_ids
    assert (
        client.get(BASE + "/sessions?scope=archived").json()["items"][0]["id"]
        == old["id"]
    )
    assert client.post(BASE + f"/sessions/{old['id']}/resume").status_code == 409

    async with dbs() as db:
        assert (await db.get(StrategyHead, owner)).strategy_version_id == previous_head
        allocations = []
        for value in data["sessions"]:
            sid = UUID(value["id"])
            row, control = (
                await db.get(PaperSession, sid),
                await db.get(PaperControl, sid),
            )
            assert row.initial_equity_cad == Decimal(10000)
            assert row.started_at == at
            assert control.classification == "RESEARCH"
            assert control.status == (
                "PAUSED"
                if control.settings["strategy_lab_profile"] == "NO_TRADING"
                else "ACTIVE"
            )
            settings = (await db.get(StrategyVersion, row.strategy_version_id)).settings
            assert settings == control.settings
            assert Decimal(settings["trade_amount_cad"]) == Decimal(500)
            assert Decimal(settings["min_buy_capital_cad"]) == Decimal(50)
            assert cohort.cohort_strategy() == cohort.PaperStrategy.model_validate(
                {
                    **settings,
                    "strategy_lab_profile": "HYBRID_OPTIMIZER",
                    "allowed_assets": [],
                    "allowed_exchanges": [],
                }
            )
            allocations.append(
                {
                    tuple(
                        (
                            x[key]
                            for key in (
                                "asset_id",
                                "exchange_id",
                                "quantity",
                                "initial_cad_value",
                            )
                        )
                    )
                    for x in row.benchmark_snapshot["holdings"]
                }
            )
            movements = list(
                await db.scalars(
                    select(AccountingMovement).where(
                        AccountingMovement.session_id == sid
                    )
                )
            )
            assert all(m.event_type == "INITIAL_FUNDING" for m in movements)
            assert sum(m.external_flow_cad for m in movements) == Decimal(10000)
            mark = await db.scalar(
                select(PortfolioCheckpoint).where(PortfolioCheckpoint.session_id == sid)
            )
            assert mark.as_of == at and Decimal(mark.payload["equity_cad"]) == Decimal(
                10000
            )
            assert Decimal(mark.payload["no_trading_benchmark_cad"]) == Decimal(10000)
            assert await reconcile(db, sid)
        assert all(rows == allocations[0] for rows in allocations)
        assert not await db.scalar(
            select(PaperTrade.id).where(
                PaperTrade.session_id.in_(
                    [UUID(session_id) for session_id in expected_ids]
                )
            )
        )
