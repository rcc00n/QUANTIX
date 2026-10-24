"""Paid escalation provider boundaries. No paid client or billable operation is enabled."""

from arbiq.notifications.urgent import UrgentNotification


class ProviderDisabled(RuntimeError):
    pass


class DisabledPaidProvider:
    def __init__(self, name: str):
        self.name = name
        self.enabled = False

    async def deliver(self, notification: UrgentNotification) -> str:
        raise ProviderDisabled(f"{self.name.upper()}_NOT_AUTHORIZED")

    async def cancel(self, provider_delivery_id: str) -> None:
        raise ProviderDisabled(f"{self.name.upper()}_NOT_AUTHORIZED")

    async def acknowledge(self, provider_delivery_id: str, actor_id) -> None:
        raise ProviderDisabled(f"{self.name.upper()}_NOT_AUTHORIZED")


push_provider = DisabledPaidProvider("push")
voice_provider = DisabledPaidProvider("voice")
