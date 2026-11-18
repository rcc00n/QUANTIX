"""Authenticated paper interfaces with SYNTHETIC isolated funds, never live executions."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from arbiq.db.models import Asset, Exchange, Journal, LedgerEntry, PaperSession, StrategyVersion
from arbiq.main import create_app
from arbiq.paper_trading.models import PaperControl, RebalanceRequest, StrategyHead
from arbiq.paper_trading.service import create_portfolio
from arbiq.portfolio.accounting import reconcile
from arbiq.portfolio.models import PortfolioPosition
from arbiq.settings.strategy import presets, strategy_lab_settings
from conftest import SYNTHETIC_PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_stage8a1_rollout import rollout


@pytest.fixture
async def private_api(database_url, app_settings, monkeypatch):
    now = datetime(2026, 11, 22, 6, tzinfo=UTC)

    async def clock(request):
        return now

    monkeypatch.setattr("arbiq.paper_trading.api.now_checked", clock)
    suffix = uuid4().hex[:10]
    settings = app_settings.model_copy(update={"admin_username": "synthetic-paper-" + suffix})
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        for slug in ("kraken", "ndax"):
            if not await db.scalar(select(Exchange).where(Exchange.slug == slug)):
                db.add(Exchange(slug=slug, name="SYNTHETIC " + slug))
        if not await db.scalar(select(Asset).where(Asset.canonical_key == "fiat:CAD")):
            db.add(
                Asset(
                    canonical_key="fiat:CAD",
                    symbol="CAD",
                    kind="fiat",
                    identifiers={"synthetic": True},
                )
            )
        asset = Asset(
            canonical_key="synthetic-api:" + suffix,
            symbol="SYNTH",
            kind="crypto",
            identifiers={"synthetic": True},
        )
        db.add(asset)
        await db.commit()
        asset_key = asset.canonical_key
    with TestClient(create_app(settings), client=("synthetic-paper-api-" + suffix, 51000)) as c:
        assert (
            c.post(
                "/api/v1/auth/login",
                json={
                    "username": settings.admin_username,
                    "password": SYNTHETIC_PASSWORD,
                },
            ).status_code
            == 200
        )
        yield c, sessions, asset_key
    await engine.dispose()


def test_private_paper_endpoints_require_sessions(client):
    client.cookies.clear()
    for path in ("/paper/sessions", "/paper/trades", "/strategies"):
        assert client.get("/api/v1" + path).status_code == 401


async def test_archival_releases_unperformed_transfer_once_and_preserves_history(
    private_api,
):
    c, dbs, _ = private_api
    old = c.post("/api/v1/paper/sessions", json={"name": "SYNTHETIC replace"}).json()
    kept = c.post("/api/v1/paper/sessions", json={"name": "SYNTHETIC retain"}).json()
    path = f"/api/v1/paper/sessions/{old['id']}"
    transfer = {
        "from_exchange": "kraken",
        "to_exchange": "ndax",
        "asset": "fiat:CAD",
        "quantity": "100",
        "network": "SYNTHETIC",
        "network_verified": True,
    }
    accepted = c.post(path + "/rebalance", json=transfer)
    assert accepted.status_code == 202
    request_id = UUID(accepted.json()["id"])
    sid = UUID(old["id"])
    async with dbs() as db:
        ledger = (
            select(LedgerEntry.id, LedgerEntry.quantity, LedgerEntry.cad_value)
            .join(Journal)
            .where(Journal.session_id == sid)
        )
        before = set((await db.execute(ledger)).all())
    archived = c.post(path + "/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "ARCHIVED"
    assert archived.json()["archived_at"]
    assert archived.json()["pending"] == {}
    assert c.post(path + "/archive").json() == archived.json()
    assert [s["id"] for s in c.get("/api/v1/paper/sessions").json()["items"]] == [kept["id"]]
    assert c.get("/api/v1/paper/sessions?scope=archived").json()["items"] == [
        {key: value for key, value in archived.json().items() if key != "pending"}
    ]
    assert c.get("/api/v1/paper/sessions?scope=all").json()["total"] == 2
    assert c.get(path + "/portfolio").json()["archived_at"] == archived.json()["archived_at"]
    assert c.get(path + "/metrics").status_code == 200
    assert c.post(path + "/resume").status_code == 409
    assert c.post(path + "/rebalance", json=transfer).status_code == 409
    assert c.put(path + "/strategy", json=old["settings"]).status_code == 409
    async with dbs() as db:
        assert before < set((await db.execute(ledger)).all())
        request = await db.get(RebalanceRequest, request_id)
        assert request.status == "CANCELLED" and request.result["reason"] == "PORTFOLIO_ARCHIVE"
        events = list(await db.scalars(select(Journal.event_type).where(Journal.session_id == sid)))
        assert events.count("RESERVATION_RELEASE") == 1
        assert "TRANSFER" not in events
        positions = (
            await db.execute(
                select(PortfolioPosition.quantity, PortfolioPosition.reserved_quantity).where(
                    PortfolioPosition.session_id == sid
                )
            )
        ).all()
        assert positions == [(Decimal("5000"), Decimal("0"))] * 2
        assert await reconcile(db, sid)


async def test_pausing_lab_keeps_its_history_and_satisfies_stage8c_guard(private_api):
    c, dbs, _ = private_api
    legacy = c.post("/api/v1/paper/sessions", json={}).json()
    async with dbs() as db:
        owner = (await db.get(PaperSession, UUID(legacy["id"]))).user_id
        lab, _ = await create_portfolio(
            db,
            owner,
            "SYNTHETIC rollback trial",
            strategy_lab_settings("HYBRID_OPTIMIZER"),
            Decimal("10000"),
            None,
            datetime(2026, 11, 22, 6, tzinfo=UTC),
            lab_cohort={"id": str(uuid4())},
        )
        lab_id = lab.id
        await db.commit()

    async def guard_result():
        # Scope the unchanged deployed SQL to this owner without touching other fixtures.
        scopes = {
            "paper_controls": (
                "SELECT c.* FROM public.paper_controls c JOIN public.paper_sessions s "
                "ON c.session_id = s.id WHERE s.user_id = :owner"
            ),
            "paper_trades": (
                "SELECT t.* FROM public.paper_trades t JOIN public.paper_sessions s "
                "ON t.session_id = s.id WHERE s.user_id = :owner"
            ),
            "strategy_heads": "SELECT * FROM public.strategy_heads WHERE user_id = :owner",
            "strategy_versions": "SELECT * FROM public.strategy_versions WHERE user_id = :owner",
        }
        async with dbs() as db:
            for table, scope in scopes.items():
                await db.execute(text(f"CREATE TEMP TABLE {table} (LIKE public.{table})"))
                await db.execute(text(f"INSERT INTO pg_temp.{table} {scope}"), {"owner": owner})
            return await db.scalar(text(rollout.STAGE8C_PAPER_COMPATIBILITY))

    assert await guard_result() == "incompatible"
    assert c.post(f"/api/v1/paper/sessions/{lab_id}/pause").status_code == 200
    assert await guard_result() == "compatible"
    async with dbs() as db:
        head = await db.get(StrategyHead, owner)
        assert str(head.strategy_version_id) == legacy["strategy_version_id"]
        session = await db.get(PaperSession, lab_id)
        pinned = await db.get(StrategyVersion, session.strategy_version_id)
        assert pinned.settings["strategy_lab_profile"] == "HYBRID_OPTIMIZER"
        assert await reconcile(db, lab_id)


async def test_funded_portfolios_and_mode_isolation(private_api):
    c, dbs, asset = private_api
    primary = c.post(
        "/api/v1/paper/sessions",
        json={"name": "SYNTHETIC validated", "starting_capital_cad": "10000"},
    )
    assert primary.status_code == 201, primary.text
    p = primary.json()
    assert p["classification"] == "VALIDATED"
    research = c.post(
        "/api/v1/paper/sessions",
        json={
            "name": "SYNTHETIC research",
            "starting_capital_cad": "10000",
            "classification": "RESEARCH",
            "allocations": [
                {
                    "exchange": "kraken",
                    "asset": asset,
                    "quantity": "50",
                    "cad_rate": "100",
                },
                {"exchange": "ndax", "quantity": "5000"},
            ],
        },
    )
    assert research.status_code == 201, research.text
    r = research.json()
    assert r["classification"] == "RESEARCH" and r["id"] != p["id"]
    listing = c.get("/api/v1/paper/sessions").json()
    assert listing["total"] == 2 and listing["scenarios_are_separate"]
    illegal = c.put(
        f"/api/v1/paper/sessions/{r['id']}/strategy",
        json=presets()["Balanced"].model_dump(mode="json"),
    )
    assert illegal.status_code == 409
    bad = c.post(
        "/api/v1/paper/sessions",
        json={
            "name": "SYNTHETIC freecrypto",
            "classification": "RESEARCH",
            "starting_capital_cad": "10000",
            "allocations": [
                {
                    "exchange": "kraken",
                    "asset": asset,
                    "quantity": "50",
                    "cad_rate": "100",
                }
            ],
        },
    )
    assert bad.status_code == 422
    async with dbs() as db:
        from uuid import UUID

        assert await reconcile(db, UUID(p["id"])) and await reconcile(db, UUID(r["id"]))


async def test_strategy_versioning_preserves_initial_configuration(private_api):
    c, dbs, _ = private_api
    p = c.post("/api/v1/paper/sessions", json={}).json()
    settings = p["settings"]
    settings["reaction_delay_seconds"] = 120
    settings["minimum_profit_cad"] = "12"
    response = c.put(f"/api/v1/paper/sessions/{p['id']}/strategy", json=settings)
    assert response.status_code == 200, response.text
    assert response.json()["strategy_version_id"] != p["strategy_version_id"]
    async with dbs() as db:
        from uuid import UUID

        session = await db.get(PaperSession, UUID(p["id"]))
        control = await db.get(PaperControl, session.id)
        old = await db.get(StrategyVersion, session.strategy_version_id)
        assert (
            str(old.id) == p["strategy_version_id"] and old.settings["reaction_delay_seconds"] == 30
        )
        assert control.settings["reaction_delay_seconds"] == 120
    settings["starting_capital_cad"] = "20000"
    assert c.put(f"/api/v1/paper/sessions/{p['id']}/strategy", json=settings).status_code == 409
    assert c.post(f"/api/v1/paper/sessions/{p['id']}/pause").json()["status"] == "PAUSED"
    assert c.post(f"/api/v1/paper/sessions/{p['id']}/resume").json()["status"] == "ACTIVE"


async def test_empty_metrics_pagination_and_insufficient_rebalance(private_api):
    c, _, _ = private_api
    p = c.post("/api/v1/paper/sessions", json={}).json()
    sid = p["id"]
    metrics = c.get(f"/api/v1/paper/sessions/{sid}/metrics")
    assert metrics.status_code == 200 and metrics.json()["win_rate_pct"] is None
    assert c.get("/api/v1/paper/trades?limit=101").status_code == 422
    assert c.get(f"/api/v1/paper/trades?session_id={sid}").json()["total"] == 0
    assert c.get("/api/v1/paper/trades/" + str(uuid4())).status_code == 404
    assert (
        c.post(
            f"/api/v1/paper/sessions/{sid}/rebalance",
            json={
                "from_exchange": "kraken",
                "to_exchange": "ndax",
                "asset": "fiat:CAD",
                "quantity": "6000",
                "network": "SYNTHETIC",
                "network_verified": True,
                "settlement_seconds": 10,
            },
        ).status_code
        == 409
    )
    assert c.get(f"/api/v1/paper/sessions/{sid}/daily").json()["timezone"] == "America/Edmonton"


async def test_rebalance_preview_keeps_research_balances_and_ledger_unchanged(
    private_api, monkeypatch
):
    from uuid import UUID

    from arbiq.db.models import LedgerEntry
    from arbiq.paper_trading.models import RebalanceRequest
    from sqlalchemy import func

    async def clock(request):
        return datetime(2026, 11, 22, 6, tzinfo=UTC), {"trusted": True}

    monkeypatch.setattr("arbiq.paper_trading.api.utc_reference", clock)
    c, dbs, _ = private_api
    p = c.post("/api/v1/paper/sessions", json={"classification": "RESEARCH"}).json()
    sid = UUID(p["id"])
    async with dbs() as db:
        before = await db.scalar(select(func.count()).select_from(LedgerEntry))
    result = c.post(
        f"/api/v1/paper/sessions/{sid}/rebalance/preview",
        json={
            "from_exchange": "kraken",
            "to_exchange": "ndax",
            "asset": "fiat:CAD",
            "target_destination_quantity": "5500",
            "network": "SYNTHETIC settlement",
            "withdrawal_available": True,
            "deposit_available": True,
            "withdrawal_fee_quantity": "1",
            "network_fee_quantity": "2",
            "settlement_seconds": 100,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["hypothetical_transfer"]["transfer_fee_cad"] == "3"
    assert result.json()["execution_performed"] is False
    async with dbs() as db:
        assert await db.scalar(select(func.count()).select_from(LedgerEntry)) == before
        assert (
            await db.scalar(select(RebalanceRequest.id).where(RebalanceRequest.session_id == sid))
            is None
        )
        assert await reconcile(db, sid)


async def test_trade_export_contains_bounded_ledger_provenance(private_api):
    from uuid import UUID

    from arbiq.db.models import PaperTrade, SimulationEvent

    c, dbs, _ = private_api
    p = c.post("/api/v1/paper/sessions", json={}).json()
    now = datetime(2026, 11, 22, 6, tzinfo=UTC)
    async with dbs() as db:
        trade = PaperTrade(
            session_id=UUID(p["id"]),
            opportunity_id=None,
            status="MISSED",
            classification="VALIDATED",
            mode="PRE_FUNDED",
            strategy_version_id=UUID(p["strategy_version_id"]),
            requested_at=now,
            reaction_delay_ms=30000,
            eligible_at=now,
            completed_at=now,
            reason="SYNTHETIC_MISSING_EVIDENCE",
            calculation_version="SYNTHETIC",
            assumptions={"synthetic": True},
            state={},
        )
        db.add(trade)
        await db.flush()
        # SYNTHETIC same-UTC events deliberately inserted in reverse logical order.
        for event in ("SCHEDULED", "ELIGIBILITY_CHECK", "CANDIDATE"):
            db.add(
                SimulationEvent(
                    trade_id=trade.id,
                    event_type=event,
                    reason="SYNTHETIC_TIED_TIMESTAMP",
                    occurred_at=now,
                    provenance={"synthetic": True},
                )
            )
        await db.commit()
        tid = trade.id
    response = c.get("/api/v1/paper/trades/" + str(tid))
    assert response.status_code == 200
    value = response.json()
    assert value["schema_version"] == "paper-trade-evidence-v1"
    assert value["ledger_entries"] == [] and value["observations"] == []
    assert value["session_provenance"]["initial_equity_cad"] == "10000.000000000000000000"
    assert value["paper_only"] is True
    assert [e["event_type"] for e in value["events"]] == [
        "CANDIDATE",
        "ELIGIBILITY_CHECK",
        "SCHEDULED",
    ]
