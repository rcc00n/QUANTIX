"""Conservative asset identities. Unknown tokens stay exchange-scoped."""

import re
from dataclasses import dataclass

FIAT = {"CAD", "USD", "EUR", "GBP", "JPY", "AUD", "CHF", "NZD", "BRL", "MXN", "KRW"}
VERIFIED = {
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "LTC",
    "USDC",
    "USDT",
    "ADA",
    "DOGE",
    "BCH",
    "LINK",
    "DOT",
    "AVAX",
    "XLM",
    "TRX",
    "ATOM",
    "ETC",
    "ALGO",
    "NEAR",
    "APT",
    "SUI",
}
UNSAFE_IDENTITY = re.compile(
    r"wrapped|bridged|synthetic|staked|wormhole|leveraged|liquid.stak", re.I
)


@dataclass(frozen=True, slots=True)
class AssetIdentity:
    key: str
    symbol: str
    kind: str
    verified: bool
    reason: str


