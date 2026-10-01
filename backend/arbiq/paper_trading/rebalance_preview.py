"""Read-only research scenario. Nothing here can reserve or move inventory."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field

from arbiq.finance.contracts import Contract, NonNegativeDecimal, PositiveDecimal, utc_timestamp
from arbiq.market_data.quality import safe_json
from arbiq.portfolio.contracts import decimal_context


class RebalancePreview(Contract):
    from_exchange: str = Field(min_length=1, max_length=80)
    to_exchange: str = Field(min_length=1, max_length=80)
    asset: str = Field(min_length=1, max_length=200)
    target_destination_quantity: PositiveDecimal
    minimum_source_available: NonNegativeDecimal = "0"
    network: str | None = Field(default=None, min_length=1, max_length=80)
    withdrawal_available: bool | None = None
    deposit_available: bool | None = None
    withdrawal_fee_quantity: NonNegativeDecimal | None = None
    network_fee_quantity: NonNegativeDecimal | None = None
    settlement_seconds: int | None = Field(default=None, ge=1, le=604800)
    conversion_required: bool = False
    conversion_cost_cad: NonNegativeDecimal | None = None


@decimal_context
def estimate_transfer(
    request: RebalancePreview,
    *,
    source_available: Decimal,
    destination_available: Decimal,
    decision_at: datetime,
) -> dict:
    """Compare a same-asset transfer assumption with leaving both balances alone."""
    utc_timestamp(decision_at)
    if request.from_exchange == request.to_exchange:
        raise ValueError("Distinct transfer venues required")
    if source_available < 0 or destination_available < 0:
        raise ValueError("Available balances must be nonnegative")
    zero = Decimal(0)
    deficit = max(zero, request.target_destination_quantity - destination_available)
    known_fees = (
        request.withdrawal_fee_quantity is not None and request.network_fee_quantity is not None
    )
    total_fees = (
        request.withdrawal_fee_quantity + request.network_fee_quantity if known_fees else None
    )
    blockers = []
    if not request.network:
        blockers.append("TRANSFER_NETWORK_UNKNOWN")
    if request.withdrawal_available is not True:
        blockers.append(
            "SOURCE_WITHDRAWAL_UNAVAILABLE"
            if request.withdrawal_available is False
            else "SOURCE_WITHDRAWAL_UNKNOWN"
        )
    if request.deposit_available is not True:
        blockers.append(
            "DESTINATION_DEPOSIT_UNAVAILABLE"
            if request.deposit_available is False
            else "DESTINATION_DEPOSIT_UNKNOWN"
        )
    if total_fees is None:
        blockers.append("TRANSFER_FEES_UNKNOWN")
    if request.settlement_seconds is None:
        blockers.append("SETTLEMENT_DELAY_UNKNOWN")
    if request.conversion_required:
        if request.conversion_cost_cad is None:
            blockers.append("CONVERSION_COST_UNKNOWN")
        blockers.append("CROSS_ASSET_CONVERSION_REQUIRES_SEPARATE_EXECUTION_AND_FX_EVIDENCE")

    status = "NO_REBALANCE_REQUIRED"
    scenario = None
    if deficit:
        status = "BLOCKED" if blockers else "RESEARCH_TRANSFER_ESTIMATE"
        if not blockers:
            movable = max(zero, source_available - request.minimum_source_available - total_fees)
            delivered = min(deficit, movable)
            if not delivered:
                status = "BLOCKED"
                blockers.append("INSUFFICIENT_UNRESERVED_SOURCE_AFTER_FEES_AND_BUFFER")
            else:
                scenario = {
                    "source_debit_quantity": delivered + total_fees,
                    "fee_quantity": total_fees,
                    "delivered_quantity_after_settlement": delivered,
                    "source_remaining_quantity": source_available - delivered - total_fees,
                    "destination_quantity_before_settlement": destination_available,
                    "destination_quantity_after_settlement": destination_available + delivered,
                    "remaining_destination_deficit": deficit - delivered,
                    "estimated_available_at": decision_at
                    + timedelta(seconds=request.settlement_seconds),
                    # Only exact CAD identity can price these hypothetical fees without
                    # a source- and quantity-specific conversion quote at settlement.
                    "transfer_fee_cad": total_fees if request.asset == "fiat:CAD" else None,
                    "conversion_cost_cad": zero,
                    "expected_pnl_cad": None,
                }
    return safe_json(
        {
            "schema_version": "quantix-rebalance-preview-v1",
            "decision_at": decision_at,
            "status": status,
            "paper_only": True,
            "research_only": True,
            "execution_performed": False,
            "network_evidence_class": "OWNER_RESEARCH_ASSUMPTIONS",
            "request": request.model_dump(mode="json"),
            "no_rebalance_benchmark": {
                "source_available_quantity": source_available,
                "destination_available_quantity": destination_available,
                "destination_deficit": deficit,
                "fee_quantity": zero,
                "expected_pnl_cad": None,
            },
            "hypothetical_transfer": scenario,
            "blockers": blockers if deficit else [],
            "risks": [
                "WITHDRAWAL_DEPOSIT_AND_NETWORK_ASSUMPTIONS_REQUIRE_INDEPENDENT_CHECK",
                "CONVERSION_RATE_AND_FUTURE_EXECUTABLE_DEPTH_UNCERTAIN",
                "INVENTORY_CAN_BE_UNAVAILABLE_UNTIL_SETTLEMENT",
                "DESTINATION_FUNDING_COVERAGE_DOES_NOT_IMPLY_ACTIONABILITY_OR_PROFIT",
            ],
        }
    )
