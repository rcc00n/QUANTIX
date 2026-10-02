"""SYNTHETIC rebalance alternatives. Preview never posts a movement or reserve."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from arbiq.paper_trading.rebalance_preview import RebalancePreview, estimate_transfer

NOW = datetime(2026, 11, 23, 1, tzinfo=UTC)
D = Decimal


def scenario(**kwargs):
    body = {
        "from_exchange": "synthetic-source",
        "to_exchange": "synthetic-destination",
        "asset": "asset:BTC",
        "target_destination_quantity": "7",
        **kwargs,
    }
    return estimate_transfer(
        RebalancePreview(**body),
        source_available=D(10),
        destination_available=D(1),
        decision_at=NOW,
    )


def test_unknown_network_availability_fees_and_settlement_block_transfer():
    data = scenario(withdrawal_available=False)
    assert data["status"] == "BLOCKED" and data["hypothetical_transfer"] is None
    assert {
        "SOURCE_WITHDRAWAL_UNAVAILABLE",
        "TRANSFER_FEES_UNKNOWN",
        "SETTLEMENT_DELAY_UNKNOWN",
    } <= set(data["blockers"])
    assert data["no_rebalance_benchmark"]["destination_deficit"] == "6"




