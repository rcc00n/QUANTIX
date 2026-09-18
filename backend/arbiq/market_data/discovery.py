from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from arbiq.collector.config import CollectorConfig
from arbiq.finance.contracts import exact_decimal
from arbiq.market_data.identity import AssetIdentity, asset_identity


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    native_symbol: str
    native_id: str
    base: AssetIdentity
    quote: AssetIdentity
    active: bool | None
    amount_step: Decimal | None
    price_tick: Decimal | None
    min_notional: Decimal | None
    quote_volume: Decimal | None
    metadata: dict
    id: UUID | None = None


