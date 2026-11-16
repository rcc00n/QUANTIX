# ruff: noqa: F811
"""Explicitly SYNTHETIC retained-data exports. Never writes live application records."""

import asyncio
import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from arbiq.analytics.export_jobs import ExportJobs
from arbiq.analytics.full_export import (
    EXCLUDED_TABLES,
    TABLE_GROUP,
    FullArchive,
    dataset_query,
    public_record,
)
from arbiq.db.base import Base
from arbiq.db.models import PaperSession, User
from test_paper_api import private_api  # noqa: F401


def test_explicit_schema_policy_and_private_projection():
    assert set(Base.metadata.tables) == TABLE_GROUP.keys() | EXCLUDED_TABLES.keys()
    owner = uuid4()
    columns, query, scoped = dataset_query(
        Base.metadata.tables["notification_outbox"], owner
    )
    names = {c.name for c in columns}
    assert scoped and not names & {
        "message",
        "binding_id",
        "user_id",
        "provider_message_id",
    }
    assert "WHERE" in str(query)
    for name in (
        "paper_fills",
        "ledger_entries",
        "paper_controls",
        "portfolio_positions",
        "notification_events",
    ):
        assert dataset_query(Base.metadata.tables[name], owner)[2], name
    assert not dataset_query(Base.metadata.tables["arbitrage_evaluations"], owner)[2]


def test_exact_private_record_sanitization():
    value = public_record(
        {
            "quantity": Decimal("-0.000000000000000123"),
            "missing": None,
            "chat_id": 123,
            "nested": {"password": "SYNTHETIC-SECRET", "message": "PRIVATE"},
            "url": "https://example.invalid/?token=secret",
            "email": "private@example.invalid",
            "source_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
        }
    )
    text = json.dumps(value)
    assert value["quantity"] == "-0.000000000000000123"
    assert value["missing"] is None
    assert all(
        secret not in text
        for secret in ("SYNTHETIC-SECRET", "PRIVATE", "private@example", "123456789:")
    )


async def test_full_stream_exceeds_legacy_row_cap_and_preserves_exact_values(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("arbiq.analytics.full_export.MIN_FREE_BYTES", 0)
    # Smaller batches still exercise cursor-to-disk flow; no legacy100000row cap.
    monkeypatch.setattr("arbiq.analytics.full_export.BATCH", 256)
    progress = []

    async def update(name, rows, size, completed=False):
        if completed:
            progress.append(rows)

    async def rows():
        for i in range(100_001):
            yield {
                "id": i,
                "quantity": Decimal("0.000000000000000123"),
                "missing": None,
            }

    path = tmp_path / "synthetic-large.zip"
    archive = FullArchive(path, update)
    try:
        await archive.csv("synthetic.csv", ["id", "quantity", "missing"], rows())
    finally:
        archive.close()
    with zipfile.ZipFile(path) as z:
        with z.open("synthetic.csv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline=""))
            count = 0
            for row in reader:
                assert (
                    row["quantity"] == "0.000000000000000123" and row["missing"] == ""
                )
                count += 1
        assert count == 100_001 and progress == [count]
        assert z.testzip() is None


def test_job_restart_recovery_and_ownership(tmp_path):
    from fastapi import HTTPException

    jobs = ExportJobs(None, tmp_path)
    owner, other = uuid4(), uuid4()
    job = {"id": str(uuid4()), "owner": str(owner), "status": "running"}
    jobs.save(job)
    partial = tmp_path / f"{job['id']}.partial"
    partial.write_bytes(b"SYNTHETIC incomplete archive")
    with pytest.raises(HTTPException) as exc:
        jobs.load(job["id"], other)
    assert exc.value.status_code == 404
    descriptor = jobs.lock()
    assert descriptor is not None and jobs.lock() is None
    jobs.cleanup_and_recover()
    import os

    os.close(descriptor)
    assert jobs.load(job["id"], owner)["status"] == "failed"
    assert not partial.exists()


async def test_complete_job_zip_all_portfolios_reconciliation_and_auth(
    private_api, tmp_path, monkeypatch
):
    from sqlalchemy import select

    c, dbs, _ = private_api
    monkeypatch.setattr("arbiq.analytics.full_export.MIN_FREE_BYTES", 0)
    monkeypatch.setattr("arbiq.analytics.export_jobs.MIN_FREE_BYTES", 0)
    jobs = ExportJobs(c.app, tmp_path)
    c.app.state.export_jobs = jobs

    async def clock(request):
        return datetime(2026, 11, 22, 8, tzinfo=UTC), {
            "trusted": True,
            "synthetic": True,
        }

    monkeypatch.setattr("arbiq.analytics.export_jobs.utc_reference", clock)
    portfolio_ids = []
    for classification in ("VALIDATED", "RESEARCH"):
        r = c.post(
            "/api/v1/paper/sessions",
            json={
                "name": "SYNTHETIC full " + classification,
                "classification": classification,
            },
        )
        assert r.status_code == 201, r.text
        portfolio_ids.append(r.json()["id"])
    async with dbs() as db:
        stranger = User(
            username="SYNTHETIC-other-" + uuid4().hex,
            password_hash="UNUSABLE",
            active=False,
        )
        db.add(stranger)
        await db.flush()
        first = await db.scalar(
            select(PaperSession).where(PaperSession.id == portfolio_ids[0])
        )
        other_session = PaperSession(
            user_id=stranger.id,
            strategy_version_id=first.strategy_version_id,
            name="SYNTHETIC PRIVATE portfolio",
            started_at=datetime.now(UTC),
            initial_equity_cad=Decimal(999),
            benchmark_snapshot={},
        )
        db.add(other_session)
        await db.commit()
        private_id = str(other_session.id)
    assert (
        c.post(
            "/api/v1/reports/exports", json={"session_id": portfolio_ids[0]}
        ).status_code
        == 422
    )
    result = c.post("/api/v1/reports/exports", json={})
    assert result.status_code == 202, result.text
    job = result.json()
    assert "owner" not in job
    for _ in range(300):
        result = c.get("/api/v1/reports/exports/" + job["id"])
        job = result.json()
        if job["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.05)
    assert job["status"] == "completed", job
    response = c.get("/api/v1/reports/exports/" + job["id"] + "/download")
    assert (
        response.status_code == 200
        and response.headers["content-type"] == "application/zip"
    )
    assert "quantix-everything-" in response.headers["content-disposition"]
    assert (
        hashlib.sha256(response.content).hexdigest()
        == response.headers["x-content-sha256"]
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert b"Market pipeline timing (Stage 8A)" in z.read("README.md")
        assert z.testzip() is None
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["complete"] and manifest["filters"] is None
        assert set(manifest["portfolio_ids"]) == set(portfolio_ids)
        assert manifest["counts"]["paper_sessions"] == 2
        assert all(p["balanced"] for p in manifest["financial_reconciliation"])
        assert (
            "retention_expired_count" in manifest
            and manifest["retention_expired_count"] is None
        )
        assert all(name not in z.namelist() for name in EXCLUDED_TABLES)
        for line in z.read("checksums.sha256").decode().splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256(z.read(name)).hexdigest() == digest
        all_bytes = b"".join(z.read(n) for n in z.namelist())
        assert private_id.encode() not in all_bytes
        assert b"SYNTHETIC PRIVATE portfolio" not in all_bytes
        metrics = json.loads(z.read("analytics/metrics.json"))
        assert metrics["combined_portfolio_equity"] is None
        assert all(p["win_rate"] is None for p in metrics["portfolios"])
        assert manifest["counts"]["paper_fills"] == 0
    # Status and file remain authenticated, even after job completion.
    c.cookies.clear()
    for path in ("/current", "/" + job["id"], "/" + job["id"] + "/download"):
        assert c.get("/api/v1/reports/exports" + path).status_code == 401
    assert c.post("/api/v1/reports/exports", json={}).status_code == 401
