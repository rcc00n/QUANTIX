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


def test_rebalance_conserves_asset_after_explicit_network_fees_and_buffer():
    data = scenario(
        minimum_source_available="4",
        network="SYNTHETIC-ROLLUP",
        withdrawal_available=True,
        deposit_available=True,
        withdrawal_fee_quantity="0.5",
        network_fee_quantity="1.5",
        settlement_seconds=120,
    )
    assert data["status"] == "RESEARCH_TRANSFER_ESTIMATE" and data["execution_performed"] is False
    result = data["hypothetical_transfer"]
    assert D(result["source_debit_quantity"]) == 6
    assert D(result["delivered_quantity_after_settlement"]) == 4
    assert D(result["fee_quantity"]) == 2
    assert D(result["destination_quantity_before_settlement"]) == 1
    assert (
        D(result["source_remaining_quantity"])
        + D(result["destination_quantity_after_settlement"])
        + D(result["fee_quantity"])
        == 11
    )
    assert D(result["remaining_destination_deficit"]) == 2
    assert datetime.fromisoformat(result["estimated_available_at"]) == NOW + timedelta(minutes=2)
    assert result["transfer_fee_cad"] is None and result["expected_pnl_cad"] is None


def test_required_conversion_is_not_assumed_to_be_free_or_executable():
    data = scenario(
        network="SYNTHETIC",
        withdrawal_available=True,
        deposit_available=True,
        withdrawal_fee_quantity="0.01",
        network_fee_quantity="0.01",
        settlement_seconds=100,
        conversion_required=True,
        conversion_cost_cad="2",
    )
    assert data["status"] == "BLOCKED"
    assert data["blockers"] == [
        "CROSS_ASSET_CONVERSION_REQUIRES_SEPARATE_EXECUTION_AND_FX_EVIDENCE"
    ]
    assert data["request"]["conversion_cost_cad"] == "2"
