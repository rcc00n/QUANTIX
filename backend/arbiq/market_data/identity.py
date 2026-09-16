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


def asset_identity(exchange: str, symbol: str, currency: dict | None = None) -> AssetIdentity:
    currency = currency or {}
    symbol = symbol.upper()
    name = str(currency.get("name") or "")
    if UNSAFE_IDENTITY.search(name) or re.search(r"[.:]|^(W|ST|1000)(BTC|ETH|SOL)$", symbol):
        return AssetIdentity(
            f"exchange:{exchange}:{symbol}",
            symbol,
            "crypto",
            False,
            "Wrapped, synthetic or incompatible asset identity",
        )
    if symbol in FIAT:
        return AssetIdentity(f"fiat:{symbol}", symbol, "fiat", True, "ISO fiat currency")
    if symbol in VERIFIED:
        kind = "stablecoin" if symbol in {"USDC", "USDT"} else "crypto"
        return AssetIdentity(
            f"asset:{symbol}",
            symbol,
            kind,
            True,
            "Curated spot identity; funding network still requires verification",
        )
    return AssetIdentity(
        f"exchange:{exchange}:{symbol}",
        symbol,
        "crypto",
        False,
        "Unverified ticker identity; no cross-exchange equivalence assumed",
    )
