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


class NotificationPreferences(Contract):
    telegram_enabled: bool = False
    minimum_buy_capital_cad: NonNegativeDecimal = "50"
    minimum_liquidity_cad: NonNegativeDecimal = "50"
    maximum_quote_age_seconds: int = Field(default=10, ge=1, le=30)
    minimum_confidence: Literal["HEALTHY"] = "HEALTHY"
    nighttime_minimum_confidence: Literal["HEALTHY"] = "HEALTHY"
    minimum_persistence_seconds: int = Field(default=0, ge=0, le=300)
    research_enabled: bool = False
    quiet_delivery: Literal["SUPPRESS", "SILENT"] = "SUPPRESS"
    minimum_profit_cad: NonNegativeDecimal = "5"
    minimum_return_pct: NonNegativeDecimal = "0.5"
    normal_alerts: bool = True
    emergency_alerts: bool = False
    timezone: Literal["America/Edmonton"] = "America/Edmonton"
    quiet_hours_enabled: bool = True
    quiet_start: LocalClock = "23:00"
    quiet_end: LocalClock = "07:00"
    nighttime_minimum_profit_cad: NonNegativeDecimal = "25"
    nighttime_minimum_return_pct: NonNegativeDecimal = "1"
    nighttime_minimum_buy_capital_cad: NonNegativeDecimal = "250"
    nighttime_minimum_liquidity_cad: NonNegativeDecimal = "500"
    nighttime_maximum_quote_age_seconds: int = Field(default=3, ge=1, le=10)
    nighttime_minimum_persistence_seconds: int = Field(default=10, ge=2, le=300)
    daily_emergency_cap: int = Field(default=5, ge=1, le=20)
    daily_report_enabled: bool = True
    daily_report_time: LocalClock = "09:00"
    weekly_report_enabled: bool = False
    weekly_report_day: int = Field(default=0, ge=0, le=6)
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)
    repeat_until_acknowledged: bool = False
    repeat_interval_seconds: int = Field(default=60, ge=30, le=3600)
    maximum_repeats: int = Field(default=5, ge=0, le=20)
    escalation_cooldown_seconds: int = Field(default=600, ge=60, le=86400)
    push_requested: bool = False
    voice_requested: bool = False
    cancel_on_expiry: Literal[True] = True
    require_manual_acknowledgment: Literal[True] = True

    @model_validator(mode="after")
    def sleep_thresholds(self):
        if (
            self.nighttime_minimum_profit_cad < self.minimum_profit_cad
            or self.nighttime_minimum_return_pct < self.minimum_return_pct
            or self.nighttime_minimum_buy_capital_cad < self.minimum_buy_capital_cad
            or self.nighttime_minimum_liquidity_cad < self.minimum_liquidity_cad
            or self.nighttime_maximum_quote_age_seconds > self.maximum_quote_age_seconds
            or self.nighttime_minimum_persistence_seconds < self.minimum_persistence_seconds
        ):
            raise ValueError("Nighttime emergency thresholds cannot be below normal thresholds")
        return self


class TerminalSettings(Contract):
    schema_version: Literal["terminal-v1"] = "terminal-v1"
    market: MarketPreferences = Field(default_factory=MarketPreferences)
    notifications: NotificationPreferences = Field(default_factory=NotificationPreferences)


async def read_effective_terminal(db, user_id=None):
    query = select(TerminalSettingsVersion)
    if user_id is not None:
        query = query.where(TerminalSettingsVersion.user_id == user_id)
    row = await db.scalar(
        query.order_by(
            TerminalSettingsVersion.version.desc(), TerminalSettingsVersion.effective_at.desc()
        ).limit(1)
    )
    return row, TerminalSettings.model_validate(row.settings) if row else TerminalSettings()


