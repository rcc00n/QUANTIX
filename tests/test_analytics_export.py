"""Explicitly SYNTHETIC records; never insert fixture fills into application data."""

# Pytest discovers the explicitly imported shared synthetic fixtures by name.
# ruff: noqa: F811

import asyncio
import csv
import hashlib
import io
import itertools
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from arbiq.analytics.contracts import CSV_COLUMNS, EXPORT_FILES, ReportPeriod
from arbiq.analytics.export import Archive, cell, make_export, sanitize
from arbiq.analytics.service import period_for
from arbiq.db.models import PaperSession, PaperTrade, StrategyVersion, User
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.contracts import PriceEvidence
from arbiq.portfolio.models import PortfolioCheckpoint, PortfolioPosition
from arbiq.portfolio.valuation import snapshot
from fastapi import HTTPException
from sqlalchemy import select
from test_paper_api import private_api  # noqa: F401
from test_paper_execution import NOW, advance, at, book, environment  # noqa: F401


def rows(archive, name):
    return list(csv.DictReader(io.StringIO(archive.read(name).decode("utf-8"))))


def test_exact_csv_and_sensitive_metadata_redaction():
    assert cell(Decimal("-0.000000000000000123")) == "-0.000000000000000123"
    assert cell("-12.50") == "-12.50"
    assert cell("0E-18", financial=True) == "0.000000000000000000"
    with pytest.raises(ValueError):
        cell(0.1, financial=True)
    assert cell('=HYPERLINK("evil")').startswith("'=")
    assert cell("\tSUM(1)").startswith("'\t")
    value = sanitize(
        {
            "api_key": "SYNTHETIC-SECRET",
            "nested": {"authorization": "SYNTH"},
            "session_id": "join-me",
            "url": "https://synthetic/?token=PRIVATE&x=1",
        }
    )
    assert "SYNTHETIC-SECRET" not in json.dumps(value)
    assert "PRIVATE" not in value["url"] and value["session_id"] == "join-me"
    value = sanitize(
        {
            "apiKey": "SYNTHETIC",
            "accessToken": "PRIVATE",
            "token_address": "public-contract-identity",
            "databaseUrl": "postgresql://owner:PRIVATE@synthetic/db",
        }
    )
    assert value["apiKey"] == value["accessToken"] == "[REDACTED]"
    assert "PRIVATE" not in value["databaseUrl"]
    assert value["token_address"] == "public-contract-identity"


def test_period_validation_rejects_naive_future_and_unbounded():
    session = type("Session", (), {"started_at": NOW})()
    for start, end in [
        (NOW.replace(tzinfo=None), NOW),
        (NOW, NOW),
        (NOW, NOW + timedelta(days=1)),
        (NOW - timedelta(days=367), NOW),
    ]:
        with pytest.raises(HTTPException):
            period_for(session, start, end, NOW)


async def test_archive_limits_fail_explicitly_without_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr("arbiq.analytics.export.MAX_ROWS_PER_FILE", 1)
    archive = Archive(tmp_path / "synthetic.zip")

    async def many():
        yield {"id": "SYNTHETIC-1"}
        yield {"id": "SYNTHETIC-2"}

    try:
        with pytest.raises(HTTPException) as exc:
            await archive.csv("test.csv", ("id",), many())
        assert exc.value.status_code == 413
    finally:
        archive.close()


def test_analytics_and_exports_require_authentication(client):
    client.cookies.clear()
    sid = uuid4()
    for path in (
        f"/analytics/sessions/{sid}",
        f"/analytics/sessions/{sid}/series",
        f"/reports/export?session_id={sid}",
    ):
        assert client.get("/api/v1" + path).status_code == 401


async def test_zero_execution_zip_headers_hashes_and_owner_isolation(
    private_api, monkeypatch
):
    c, dbs, _ = private_api
    now = datetime(2026, 11, 22, 6, 10, tzinfo=UTC)

    async def clock(request):
        return now, {"trusted": True, "host_healthy": False, "synthetic": True}

    monkeypatch.setattr("arbiq.analytics.api.utc_reference", clock)
    portfolio = c.post(
        "/api/v1/paper/sessions", json={"name": "SYNTHETIC export"}
    ).json()
    sid = portfolio["id"]
    response = c.get(f"/api/v1/reports/export?session_id={sid}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert (
        hashlib.sha256(response.content).hexdigest()
        == response.headers["x-content-sha256"]
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert b"Market pipeline timing (Stage 8A)" in z.read("README.md")
        assert set(EXPORT_FILES).issubset(z.namelist())
        for name, columns in CSV_COLUMNS.items():
            assert next(csv.reader(io.StringIO(z.read(name).decode()))) == list(columns)
        for name in ("paper_trades.csv", "paper_fills.csv", "signals.csv", "fees.csv"):
            assert rows(z, name) == []
        metrics = json.loads(z.read("metrics.json"))
        assert metrics["metrics"]["completed_simulations"] == 0
        assert metrics["metrics"]["win_rate_pct"] is None
        assert metrics["reconciliation"]["balanced"] is True
        assert metrics["classification"] == "VALIDATED"
        assert len(rows(z, "journals.csv")) == 2
        assert len(rows(z, "balances.csv")) > 0
        assert b"SYNTHETIC export" in z.read("config_snapshot.json")
        manifest = json.loads(z.read("export_manifest.json"))
        for item in manifest["files"]:
            assert hashlib.sha256(z.read(item["name"])).hexdigest() == item["sha256"]
        complete = b"".join(z.read(name) for name in z.namelist())
        assert b"password_hash" not in complete
        assert b"synthetic-fixture-password-only" not in complete
    async with dbs() as db:
        other = User(
            username="synthetic-other-export-" + uuid4().hex, password_hash="SYNTHETIC"
        )
        db.add(other)
        await db.flush()
        version = StrategyVersion(
            user_id=other.id, version=1, settings={}, reason="SYNTHETIC"
        )
        db.add(version)
        await db.commit()
        version_id = version.id
    assert c.get(f"/api/v1/analytics/sessions/{uuid4()}").status_code == 404
    assert (
        c.get(
            f"/api/v1/analytics/sessions/{sid}?strategy_version_id={version_id}"
        ).status_code
        == 404
    )
    assert (
        c.get(f"/api/v1/analytics/sessions/{sid}/series?limit=501").status_code == 422
    )


async def test_historical_analytics_never_uses_later_completion_or_valuation(
    private_api, monkeypatch
):
    c, dbs, _ = private_api
    now = datetime(2026, 11, 22, 6, 10, tzinfo=UTC)

    async def clock(request):
        return now, {"trusted": True, "synthetic": True}

    monkeypatch.setattr("arbiq.analytics.api.utc_reference", clock)
    p = c.post("/api/v1/paper/sessions", json={"name": "SYNTHETIC historical"}).json()
    sid = UUID(p["id"])
    start, cutoff = now - timedelta(minutes=10), now - timedelta(minutes=5)
    async with dbs() as db:
        db.add(
            PaperTrade(
                session_id=sid,
                opportunity_id=None,
                classification="VALIDATED",
                mode="PRE_FUNDED",
                strategy_version_id=UUID(p["strategy_version_id"]),
                status="COMPLETED",
                requested_at=start + timedelta(seconds=1),
                eligible_at=start + timedelta(seconds=31),
                reaction_delay_ms=30000,
                completed_at=now - timedelta(seconds=1),
                calculation_version="SYNTHETIC",
                assumptions={
                    "synthetic": True,
                    "strategy": {"trade_amount_cad": "100"},
                },
                state={"result": {"net_pnl_cad": "123.456789012345678901"}},
            )
        )
        for time, equity in [(start, "9999.123456789012345678"), (now, "99999")]:
            db.add(
                PortfolioCheckpoint(
                    session_id=sid,
                    as_of=time,
                    calculation_version="SYNTHETIC",
                    payload={
                        "synthetic": True,
                        "equity_cad": equity,
                        "no_trading_benchmark_cad": "10000",
                        "drawdown_pct": "0.008765432109876544",
                        "reserved_capital_cad": "0",
                        "net_simulated_pnl_cad": "-0.876543210987654322",
                    },
                )
            )
        await db.commit()
    query = {"start": start.isoformat(), "end": cutoff.isoformat()}
    response = c.get(f"/api/v1/analytics/sessions/{sid}", params=query)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["metrics"]["completed_simulations"] == 0
    assert result["metrics"]["net_completed_trade_pnl_cad"] is None
    assert result["metrics"]["win_rate_pct"] is None
    assert result["portfolio"]["equity_cad"] == "9999.123456789012345678"
    response = c.get("/api/v1/reports/export", params={"session_id": str(sid), **query})
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        trade = rows(z, "paper_trades.csv")[0]
        assert trade["status"] == "OPEN_AT_CUTOFF"
        assert "123.456789012345678901" not in trade["provenance_json"]
        equity = rows(z, "portfolio_equity.csv")
        assert len(equity) == 1 and equity[0]["equity_cad"] == "9999.123456789012345678"


async def test_series_is_bounded_and_keeps_exact_endpoints(private_api, monkeypatch):
    c, dbs, _ = private_api
    now = datetime(2026, 11, 22, 6, 10, tzinfo=UTC)

    async def clock(request):
        return now, {"trusted": True, "synthetic": True}

    monkeypatch.setattr("arbiq.analytics.api.utc_reference", clock)
    p = c.post("/api/v1/paper/sessions", json={"name": "SYNTHETIC series"}).json()
    sid = UUID(p["id"])
    async with dbs() as db:
        for i in range(21):
            db.add(
                PortfolioCheckpoint(
                    session_id=sid,
                    as_of=now - timedelta(seconds=30 - i),
                    calculation_version="SYNTHETIC",
                    payload={
                        "equity_cad": f"10000.{i:018d}",
                        "drawdown_pct": "0.000000000000000001",
                    },
                )
            )
        await db.commit()
    response = c.get(f"/api/v1/analytics/sessions/{sid}/series?limit=5")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["available_count"] == 21 and len(value["items"]) <= 5
    assert value["items"][0]["equity_cad"] == "10000.000000000000000000"
    assert value["items"][-1]["equity_cad"] == "10000.000000000000000020"
    assert isinstance(value["max_drawdown_full_period_pct"], str)


async def test_completed_fill_export_reconstructs_real_accounting(environment):
    e = environment
    await at(e, 4)
    await e.coordinator.observe(
        book(e.buy.market_id, e.buy.exchange, e.state["now"], index=3)
    )
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=4,
        )
    )
    await at(e, 6)
    await e.coordinator.observe(
        book(e.buy.market_id, e.buy.exchange, e.state["now"], index=5)
    )
    await advance(e)
    await at(e, 7)
    await e.coordinator.observe(
        book(
            e.sell.market_id,
            e.sell.exchange,
            e.state["now"],
            bid="110",
            ask="111",
            index=6,
        )
    )
    await advance(e)
    async with e.sessions() as db:
        trade = await db.get(PaperTrade, e.ids["trade"])
        assert trade.status == "COMPLETED"
        session = await db.get(PaperSession, e.ids["session"])
        control = await db.get(PaperControl, session.id)
        period = ReportPeriod(start=NOW, end=NOW + timedelta(minutes=1))
        path, _ = await make_export(
            db,
            session,
            control,
            period,
            period.end,
            {"trusted": True, "synthetic": True},
        )
    try:
        with zipfile.ZipFile(path) as z:
            assert len(rows(z, "paper_fills.csv")) == 2
            assert len(rows(z, "orders.csv")) == 2
            assert len(rows(z, "order_books.csv")) >= 2
            assert len(rows(z, "fee_versions.csv")) >= 2
            assert len(rows(z, "fx_quotes.csv")) >= 1
            assert rows(z, "paper_trades.csv")[0]["status"] == "COMPLETED"
            sums = {}
            for row in rows(z, "ledger_entries.csv"):
                sums[row["journal_id"]] = sums.get(
                    row["journal_id"], Decimal(0)
                ) + Decimal(row["quantity"])
            # Journals balance each asset separately; the scalar total is therefore zero too.
            assert all(value == 0 for value in sums.values())
            metrics = json.loads(z.read("metrics.json"))
            assert metrics["classification"] == "RESEARCH"
            assert metrics["reconciliation"]["balanced"] is True
            assert metrics["metrics"]["completed_simulations"] == 1
            assert metrics["metrics"]["by_trade_size"][0]["key"] != "unknown"
    finally:
        path.unlink(missing_ok=True)


async def test_negative_holding_return_benchmark_and_research_isolation(
    private_api, monkeypatch
):
    c, dbs, asset = private_api
    now = datetime(2026, 11, 22, 6, 10, tzinfo=UTC)

    async def clock(request):
        return now, {"trusted": True, "synthetic": True}

    monkeypatch.setattr("arbiq.analytics.api.utc_reference", clock)
    primary = c.post(
        "/api/v1/paper/sessions", json={"name": "SYNTHETIC primary"}
    ).json()
    research = c.post(
        "/api/v1/paper/sessions",
        json={
            "name": "SYNTHETIC research holding loss",
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
    ).json()
    async with dbs() as db:
        sid = UUID(research["id"])
        positions = list(
            await db.scalars(
                select(PortfolioPosition).where(PortfolioPosition.session_id == sid)
            )
        )
        marks = {
            (p.exchange_id, p.asset_id): PriceEvidence(
                cad_per_unit=Decimal(90),
                observed_at=now - timedelta(seconds=1),
                received_at=now - timedelta(seconds=1),
                verified=False,
                source="SYNTHETIC exact observed holding mark",
                provenance={"synthetic": True},
            )
            for p in positions
        }
        value = await snapshot(
            db, sid, marks, now - timedelta(seconds=1), research_only=True
        )
        assert value["equity_cad"] == "9500.000000000000000000"
        await snapshot(db, UUID(primary["id"]), {}, now - timedelta(seconds=1))
        await db.commit()
    r = c.get(f"/api/v1/analytics/sessions/{research['id']}").json()
    p = c.get(f"/api/v1/analytics/sessions/{primary['id']}").json()
    assert r["portfolio"]["equity_cad"] == r["portfolio"]["no_trading_benchmark_cad"]
    assert r["portfolio"]["unrealized_pnl_cad"] == "-500.000000000000000000"
    assert r["portfolio"]["strategy_excess_cad"] == "0E-18"
    assert p["portfolio"]["equity_cad"] == "10000.000000000000000000"
    assert r["metrics"]["win_rate_pct"] is None
    monkeypatch.setattr("arbiq.analytics.api._export_gate", asyncio.Semaphore(0))
    assert (
        c.get(f"/api/v1/reports/export?session_id={primary['id']}").status_code == 429
    )


async def test_wide_synthetic_provenance_keeps_event_loop_responsive(tmp_path):
    """Wide retained-style fields are valid CSV, not an excuse to block all API work."""
    payload = {
        "kind": "EXPLICITLY_SYNTHETIC_CPU_LOAD",
        "evidence": [
            {
                "level": i,
                "price": f"{100 + i}.000000000000000001",
                "quantity": "1.000000000000000000",
                "reason": "SYNTHETIC_EVIDENCE_ONLY_" * 4,
            }
            for i in range(2000)
        ],
    }

    async def dataset():
        for i in range(12):
            yield {"id": f"synthetic-{i}", "provenance_json": payload}

    archive = Archive(tmp_path / "synthetic-wide.zip")
    heartbeat = []
    task = asyncio.create_task(
        archive.csv("wide.csv", ("id", "provenance_json"), dataset())
    )
    while not task.done():
        heartbeat.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.005)
    await task
    archive.close()
    assert len(heartbeat) >= 3
    # Heartbeats occur while the CPU-heavy member is still being generated.
    assert max(b - a for a, b in itertools.pairwise(heartbeat)) < 0.5
    previous = csv.field_size_limit(256 * 1024 * 1024)
    try:
        with zipfile.ZipFile(tmp_path / "synthetic-wide.zip") as z:
            assert z.testzip() is None
            with z.open("wide.csv") as member:
                count = 0
                for row in csv.DictReader(
                    io.TextIOWrapper(member, encoding="utf-8", newline="")
                ):
                    assert len(row["provenance_json"]) > 131072
                    assert json.loads(row["provenance_json"]) == payload
                    count += 1
                assert count == 12
    finally:
        csv.field_size_limit(previous)


async def test_wide_immutable_evidence_export_bytes_survive_small_cursor_batches(
    environment,
    monkeypatch,
):
    """SYNTHETIC wide immutable rows cross batch boundaries without losing evidence."""
    from arbiq.db.models import ArbitrageEvaluation

    e = environment
    wide = {
        "kind": "EXPLICITLY_SYNTHETIC_WIDE_RETAINED_EVIDENCE",
        "levels": [
            {
                "price": f"{100 + i}.000000000000000001",
                "quantity": "1.000000000000000000",
                "label": "SYNTHETIC_ONLY",
            }
            for i in range(1000)
        ],
    }
    inserted = set()
    async with e.sessions() as db:
        trade = await db.get(PaperTrade, e.ids["trade"])
        original = await db.get(ArbitrageEvaluation, trade.evaluation_id)
        values = {
            c.name: getattr(original, c.name)
            for c in original.__table__.columns
            if c.name
            not in {"id", "created_at", "revision", "evaluated_at", "evidence"}
        }
        for i in range(65):
            row = ArbitrageEvaluation(
                **values,
                revision=original.revision + i + 1,
                evaluated_at=NOW + timedelta(microseconds=i + 1),
                evidence={**original.evidence, "synthetic_wide_evidence": wide},
            )
            db.add(row)
            await db.flush()
            inserted.add(str(row.id))
        await db.commit()
    paths = []
    try:
        for batch in (200, 32):
            monkeypatch.setattr("arbiq.analytics.export.BATCH_ROWS", batch)
            async with e.sessions() as db:
                session = await db.get(PaperSession, e.ids["session"])
                control = await db.get(PaperControl, session.id)
                period = ReportPeriod(start=NOW, end=NOW + timedelta(minutes=1))
                path, _ = await make_export(
                    db,
                    session,
                    control,
                    period,
                    period.end,
                    {"trusted": True, "synthetic": True},
                )
                paths.append(path)
        with zipfile.ZipFile(paths[0]) as before, zipfile.ZipFile(paths[1]) as after:
            assert before.namelist() == after.namelist()
            for name in before.namelist():
                assert before.read(name) == after.read(name), name
            previous = csv.field_size_limit(256 * 1024 * 1024)
            try:
                values = rows(after, "opportunities.csv")
            finally:
                csv.field_size_limit(previous)
            retained = [row for row in values if row["id"] in inserted]
            assert len(retained) == 65
            for row in retained:
                assert (
                    json.loads(row["provenance_json"])["evaluation"]["evidence"][
                        "synthetic_wide_evidence"
                    ]
                    == wide
                )
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


async def test_cancelled_archive_drains_inflight_thread_before_member_close(
    tmp_path, monkeypatch
):
    import threading
    import time

    started, finished = threading.Event(), threading.Event()
    original = cell

    def deliberately_slow(value, **kwargs):
        started.set()
        try:
            time.sleep(0.1)
            return original(value, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr("arbiq.analytics.export.cell", deliberately_slow)

    async def dataset():
        yield {"id": "SYNTHETIC_CANCELLATION"}

    archive = Archive(tmp_path / "synthetic-cancel.zip")
    task = asyncio.create_task(archive.csv("cancel.csv", ("id",), dataset()))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    archive.close()
    with zipfile.ZipFile(tmp_path / "synthetic-cancel.zip") as z:
        assert z.testzip() is None
        assert b"SYNTHETIC_CANCELLATION" in z.read("cancel.csv")


async def test_terminal_preferences_export_excludes_later_versions(
    private_api, monkeypatch
):
    from arbiq.settings.terminal import TerminalSettings, save_terminal

    c, dbs, _ = private_api
    now = datetime(2026, 11, 22, 6, 10, tzinfo=UTC)

    async def clock(request):
        return now, {"trusted": True, "synthetic": True}

    monkeypatch.setattr("arbiq.analytics.api.utc_reference", clock)
    p = c.post(
        "/api/v1/paper/sessions", json={"name": "SYNTHETIC settings history"}
    ).json()
    async with dbs() as db:
        session = await db.get(PaperSession, UUID(p["id"]))
        for version, minutes, assets, profile in (
            (1, 9, ["SYNTH_BTC"], "BALANCED_ADAPTIVE"),
            (2, 1, ["SYNTH_FUTURE"], "BROAD_EXPLORATION"),
        ):
            await save_terminal(
                db,
                session.user_id,
                TerminalSettings.model_validate(
                    {
                        "market": {"enabled_assets": assets, "market_profile": profile},
                        "notifications": {"telegram_enabled": False},
                    }
                ),
                now - timedelta(minutes=minutes),
                expected_version=version - 1,
            )
        await db.commit()
    response = c.get(
        "/api/v1/reports/export",
        params={
            "session_id": p["id"],
            "start": (now - timedelta(minutes=10)).isoformat(),
            "end": (now - timedelta(minutes=5)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        config = json.loads(z.read("config_snapshot.json"))
        retained = config["terminal_preferences_at_cutoff"]
        assert retained["version"] == 1
        assert retained["settings"]["market"]["market_profile"] == "BALANCED_ADAPTIVE"
        assert config["notification_delivery_active"] is False
        past = rows(z, "terminal_settings_history.csv")
        assert len(past) == 1
        assert (
            json.loads(past[0]["settings"])["market"]["market_profile"]
            == "BALANCED_ADAPTIVE"
        )
        assert b"BROAD_EXPLORATION" not in z.read("terminal_settings_history.csv")
        assert b"SYNTH_FUTURE" not in z.read("terminal_settings_history.csv")
        assert b"SYNTH_FUTURE" not in z.read("config_snapshot.json")
