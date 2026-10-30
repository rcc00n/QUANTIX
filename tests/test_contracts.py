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












