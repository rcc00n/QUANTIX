"""Reporting and book validation with explicitly SYNTHETIC fixtures."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from arbiq.ai.providers import DisabledProvider, NvidiaProvider
from arbiq.analytics.contracts import (
    CSV_COLUMNS,
    EXPORT_FILES,
    DailySummary,
    ExportManifest,
    ReportPeriod,
    SimulationCounts,
)
from arbiq.auth.security import hash_password, hash_token, verify_password
from arbiq.core.config import Settings
from arbiq.market_data.contracts import NormalizedOrderBook
from pydantic import ValidationError


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01T00:00:00", "2026-01-02T00:00:00Z"),
        ("2026-01-01T00:00:00-06:00", "2026-01-02T00:00:00Z"),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ],
)
def test_report_requires_ordered_utc_period(start, end):
    with pytest.raises(ValidationError):
        ReportPeriod(start=start, end=end)


def test_report_file_contract_and_reconciliation():
    assert EXPORT_FILES == (
        "SUMMARY.md",
        "metrics.json",
        "opportunities.csv",
        "signals.csv",
        "paper_trades.csv",
        "paper_fills.csv",
        "portfolio_equity.csv",
        "balances.csv",
        "fees.csv",
        "system_health.csv",
        "config_snapshot.json",
    )
    assert len(CSV_COLUMNS) == 8
    for headers in CSV_COLUMNS.values():
        assert len(set(headers)) == len(headers)
    with pytest.raises(ValidationError, match="reconcile"):
        SimulationCounts(
            attempts=2, pending=0, completed=1, partial=0, missed=0, failed=0
        )
    counts = SimulationCounts(
        attempts=5, pending=1, completed=1, partial=1, missed=1, failed=1
    )
    assert counts.attempts == 5
    schema = DailySummary.model_json_schema()
    assert "no_trading_benchmark_cad" in schema["properties"]
    assert "net_simulated_realized_pnl_cad" in schema["properties"]
    manifest = ExportManifest(
        generated_at=datetime.now(UTC),
        period=ReportPeriod(start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z"),
        session_ids=(uuid4(),),
    )
    assert manifest.model_dump(mode="json")["mode"] == "paper"


def synthetic_book(**changes):
    return {
        "market_id": uuid4(),
        "observed_at": datetime.now(UTC),
        "received_at": datetime.now(UTC),
        "bids": [{"price": "100", "quantity": "1"}, {"price": "99", "quantity": "2"}],
        "asks": [{"price": "101", "quantity": "1"}, {"price": "102", "quantity": "2"}],
        "quality": "valid",
        "source": "synthetic_test_fixture",
        **changes,
    }


def test_orderbook_quality_and_ordering():
    assert NormalizedOrderBook(**synthetic_book()).quality == "valid"
    for changes in [
        {"bids": []},
        {"bids": [{"price": "101", "quantity": "1"}]},
        {
            "asks": [
                {"price": "102", "quantity": "1"},
                {"price": "101", "quantity": "1"},
            ]
        },
    ]:
        with pytest.raises(ValidationError):
            NormalizedOrderBook(**synthetic_book(**changes))










