from typing import Literal
from uuid import UUID

from pydantic import model_validator

from arbiq.finance.contracts import Contract, PositiveDecimal, UTCTimestamp


class BookLevel(Contract):
    price: PositiveDecimal
    quantity: PositiveDecimal


class NormalizedOrderBook(Contract):
    market_id: UUID
    observed_at: UTCTimestamp
    received_at: UTCTimestamp
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    quality: Literal["valid", "stale", "crossed", "incomplete", "invalid"]
    source: str
    sequence: str | None = None

    @model_validator(mode="after")
    def validate_book(self) -> "NormalizedOrderBook":
        if self.quality == "valid":
            if not self.bids or not self.asks:
                raise ValueError("Valid books require both sides")
            if self.bids[0].price >= self.asks[0].price:
                raise ValueError("Valid books cannot be crossed or locked")
            if any(a.price <= b.price for a, b in zip(self.bids, self.bids[1:], strict=False)):
                raise ValueError("Bids must be strictly descending")
            if any(a.price >= b.price for a, b in zip(self.asks, self.asks[1:], strict=False)):
                raise ValueError("Asks must be strictly ascending")
        return self
