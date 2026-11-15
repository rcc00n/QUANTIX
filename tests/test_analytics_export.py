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
















