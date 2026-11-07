"""The capacity fixtures are explicitly SYNTHETIC and never imply live capacity."""

import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_stage4.py"
if not PATH.exists():
    PATH = Path("/app/scripts/benchmark_stage4.py")
spec = importlib.util.spec_from_file_location("synthetic_capacity_harness", PATH)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_capacity_evidence_variants_preserve_identical_complete_payloads():
    before = benchmark.evidence_measurement(4, False)
    after = benchmark.evidence_measurement(4, True)
    assert before["canonical_payload_equal"] and after["canonical_payload_equal"]
    assert before["kind"].startswith("EXPLICITLY_SYNTHETIC")
    assert after["variant"] == "shared_immutable"
    assert after["routes"] == 4
    json.dumps(after)


@pytest.mark.asyncio
async def test_capacity_rejects_unsupported_counts_before_allocating_or_network():
    with pytest.raises(ValueError, match="120/200/300/500"):
        await benchmark.scanner_measurement(10000, 2)


def test_capacity_market_and_book_provenance_remain_synthetic():
    market = benchmark.synthetic_market("synthetic00", 0)
    book = benchmark.synthetic_book("synthetic00", market, benchmark.FIXTURE_TIME, 1)
    assert market.metadata["synthetic"] is True
    assert market.base.key.startswith("synthetic:")
    assert book.source == "SYNTHETIC_OFFLINE_REPLAY"
    assert len(book.normalized.bids) == 25
    assert len(book.normalized.asks) == 25


@pytest.mark.asyncio
async def test_paper_capacity_rejects_non_test_database_before_connecting(monkeypatch):
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://synthetic:synthetic@localhost/production",
    )
    with pytest.raises(ValueError, match="ending _test"):
        await benchmark.paper_ledger_measurement(1)
