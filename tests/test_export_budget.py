# ruff: noqa: F811
"""Explicit SYNTHETIC export fixtures. No production records or exchange traffic."""

import asyncio
import hashlib
import json
import zipfile
from decimal import Decimal
from uuid import uuid4

import pytest
from arbiq.analytics.evidence_codec import CODEC, EvidenceStore, compact, decode
from arbiq.analytics.export_budget import ExportBudgetExceeded, verify_archive
from arbiq.analytics.export_loader import ExportReader
from arbiq.analytics.export_selection import AnalysisSelection
from arbiq.analytics.full_export import FullArchive, public_record
from test_paper_api import private_api  # noqa: F401


async def test_budget_fallback_is_bounded_and_uses_same_snapshot(tmp_path, monkeypatch):
    from arbiq.analytics.full_export import build_full_export

    calls = []
    snapshot = object()
    path = tmp_path / "synthetic.partial"

    async def attempt(db, owner, target, progress, clock, *, analysis):
        assert db is snapshot
        calls.append(analysis)
        if not analysis:
            target.write_bytes(b"SYNTHETIC rejected partial")
            raise ExportBudgetExceeded("ZIP_BUDGET")
        assert not target.exists()
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("synthetic.txt", "complete financial fixture")
        return {"coverage_level": "analysis_snapshot", "lossless_raw_history": False}

    async def progress(*args, **kwargs):
        pass

    monkeypatch.setattr("arbiq.analytics.full_export._build_full_attempt", attempt)
    manifest = await build_full_export(snapshot, uuid4(), path, progress, {})
    assert calls == [False, True] and not manifest["lossless_raw_history"]
    assert verify_archive(path) < 512_000_000


async def test_cancelled_job_cleans_files_and_releases_generator(
    private_api, tmp_path, monkeypatch
):
    import os
    from datetime import UTC, datetime

    from arbiq.analytics.api import _export_gate
    from arbiq.analytics.export_jobs import ExportJobs

    c, _, _ = private_api
    jobs = ExportJobs(c.app, tmp_path)
    c.app.state.export_jobs = jobs
    monkeypatch.setattr("arbiq.analytics.export_jobs.MIN_FREE_BYTES", 0)

    async def clock(request):
        return datetime.now(UTC), {"synthetic": True}

    async def slow(db, owner, path, progress, clock):
        for i in range(1000):
            await progress("SYNTHETIC cancellable generation", i, i)
            await asyncio.sleep(0.02)

    monkeypatch.setattr("arbiq.analytics.export_jobs.utc_reference", clock)
    monkeypatch.setattr("arbiq.analytics.export_jobs.build_full_export", slow)
    job = c.post("/api/v1/reports/exports", json={}).json()
    await asyncio.sleep(0.05)
    response = c.post(f"/api/v1/reports/exports/{job['id']}/cancel")
    assert response.status_code == 200
    for _ in range(100):
        state = c.get(f"/api/v1/reports/exports/{job['id']}").json()
        if state["status"] == "cancelled" and not _export_gate.locked():
            break
        await asyncio.sleep(0.02)
    assert state["status"] == "cancelled", state
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.evidence.sqlite"))
    assert not _export_gate.locked()
    descriptor = jobs.lock()
    assert descriptor is not None
    os.close(descriptor)
    assert c.get(f"/api/v1/reports/exports/{job['id']}/download").status_code == 409


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        None,
        False,
        "0.000000000000000123",
        {"r": 1, "m": {"a": []}},
        {"$quantix_codec": CODEC, "value": {"r": 999}},
        {"unicode": "Синтетические данные", "missing": None, "zero": "0"},
        {"books": [["12345.000000000000000001", "0.000000000000000123"]] * 100},
    ],
)
def test_json_dag_exact_and_marker_collision_safe(tmp_path, value):
    store = EvidenceStore(tmp_path / "synthetic.sqlite")
    try:
        packed = store.encode(value)
        assert decode(packed, store.lookup) == value
        before = store.nodes
        assert store.encode(value) == packed
        assert store.nodes == before
    finally:
        store.close()


def test_canonical_evidence_bytes_keep_sort_order_escaping_and_postorder_ids(tmp_path):
    store = EvidenceStore(tmp_path / "synthetic.sqlite")
    quote = 'SYNTHETIC: "\\\n\t𝄞'
    child = {"q": quote * 60, "n": -0.0}
    record = {"large": "synthetic" * 60, "an\u0000": [child, None, True, 1.25e-12]}
    try:
        result = store.encode(record)
        expected_child = compact({"m": child})
        expected_parent = compact(
            {"m": {"large": record["large"], "an\u0000": {"a": [{"r": 1}, None, True, 1.25e-12]}}}
        )
        objects = list(store.objects())
        assert [body for _, _, body in objects] == [expected_child, expected_parent]
        assert result == {"$quantix_codec": CODEC, "value": {"r": 2}}
        assert decode(result, store.lookup) == record
    finally:
        store.close()


def test_archive_limit_checks_actual_closed_size_and_crc(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("synthetic.txt", "synthetic data")
    actual = path.stat().st_size
    monkeypatch.setattr("arbiq.analytics.export_budget.MAX_ZIP_BYTES", actual)
    assert verify_archive(path) == actual
    monkeypatch.setattr("arbiq.analytics.export_budget.MAX_ZIP_BYTES", actual - 1)
    with pytest.raises(ExportBudgetExceeded):
        verify_archive(path)
    monkeypatch.setattr("arbiq.analytics.export_budget.MAX_ZIP_BYTES", 512_000_000)
    # A corrupt stored member must not be published even when the size is small.
    body = path.read_bytes().replace(b"synthetic data", b"SYNTHETIC DATA")
    path.write_bytes(body)
    with pytest.raises((RuntimeError, zipfile.BadZipFile)):
        verify_archive(path)






