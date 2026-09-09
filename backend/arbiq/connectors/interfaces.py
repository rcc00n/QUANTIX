from typing import Protocol


class PublicMarketDataConnector(Protocol):
    """Read-only interface. No order placement or authenticated trading methods.

    CCXT discovery and rate-limited collection are implemented in ccxt_public. All calls must
    preserve native identifiers, exchange timestamps and data quality flags.
    """

    exchange_id: str

    async def discover_spot_markets(self) -> list[dict[str, object]]: ...
    async def fetch_raw_book(self, native_symbol: str) -> dict: ...
    async def close(self) -> None: ...
