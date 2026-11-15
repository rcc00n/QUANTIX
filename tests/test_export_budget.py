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














