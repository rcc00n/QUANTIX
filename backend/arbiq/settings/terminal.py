"""Persistent, versioned non-secret operator preferences."""

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from arbiq.db.models import User
from arbiq.finance.contracts import Contract, NonNegativeDecimal
from arbiq.notifications.urgent import LocalClock
from arbiq.settings.models import TerminalSettingsVersion


class MarketPreferences(Contract):
    enabled_exchanges: tuple[str, ...] = ()
    enabled_assets: tuple[str, ...] = ()
    preferred_quote_currencies: tuple[str, ...] = ("CAD", "USD", "USDC", "USDT")
    cad_focused: bool = True
    research_anomaly_scanning: bool = True
    maximum_age_seconds: int = Field(default=30, ge=1, le=60)
    minimum_liquidity: NonNegativeDecimal = "0"
    spot_only: Literal[True] = True
    maximum_markets_per_exchange: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def bounded(self):
        for values, limit in (
            (self.enabled_exchanges, 30),
            (self.enabled_assets, 100),
            (self.preferred_quote_currencies, 20),
        ):
            if len(values) > limit or len(values) != len(set(values)):
                raise ValueError("Preference lists must be bounded and unique")
            if any(not value or len(value) > 80 for value in values):
                raise ValueError("Invalid market preference value")
        return self


