"""Stage 6 escalation contracts; paid provider implementations stay disabled."""

from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from arbiq.finance.contracts import Contract, NonNegativeDecimal, UTCTimestamp

LocalClock = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class NotificationChannel(StrEnum):
    TELEGRAM = "telegram"
    EMERGENCY_PUSH = "emergency_push"
    VOICE = "voice"


class UrgencyPolicy(Contract):
    enabled: bool = False
    timezone: Literal["America/Edmonton"] = "America/Edmonton"
    quiet_start: LocalClock = "23:00"
    quiet_end: LocalClock = "07:00"
    daytime_minimum_cad: NonNegativeDecimal = "5"
    nighttime_minimum_cad: NonNegativeDecimal = "25"
    daytime_minimum_pct: NonNegativeDecimal = "0.5"
    nighttime_minimum_pct: NonNegativeDecimal = "1"
    minimum_quote_liquidity: NonNegativeDecimal = "10000"
    minimum_confidence: Literal["HEALTHY"] = "HEALTHY"
    repeat_interval_seconds: int = Field(default=60, ge=30)
    escalation_cooldown_seconds: int = Field(default=600, ge=60)
    maximum_repeats: int = Field(default=5, ge=0, le=20)
    cancel_on_expiry: Literal[True] = True
    require_manual_acknowledgment: Literal[True] = True
    voice_enabled: Literal[False] = False

    @model_validator(mode="after")
    def higher_sleep_thresholds(self):
        if (
            self.nighttime_minimum_cad < self.daytime_minimum_cad
            or self.nighttime_minimum_pct < self.daytime_minimum_pct
        ):
            raise ValueError("Sleeping-hour thresholds cannot be lower than daytime thresholds")
        return self


class UrgentNotification(Contract):
    opportunity_id: UUID
    signal_id: UUID
    expires_at: UTCTimestamp
    deduplication_key: str
    acknowledgment_token_hash: str  # Never put raw acknowledgment tokens in logs/exports.
    message: str


class UrgentProvider(Protocol):
    async def deliver(self, notification: UrgentNotification) -> str: ...
    async def cancel(self, provider_delivery_id: str) -> None: ...
    async def acknowledge(self, provider_delivery_id: str, actor_id: UUID) -> None: ...
