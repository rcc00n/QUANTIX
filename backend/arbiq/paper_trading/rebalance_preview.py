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


