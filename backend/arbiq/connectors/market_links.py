"""Explicit official market screens checked 2026-11-22. Never interpolate symbols.

Each exact page returned a matching Kraken Pro spot base/quote title. Other routes
remain unavailable until similarly corroborated; this is not eligibility approval.
"""

VERIFIED_MARKET_LINKS = {
    ("kraken", "BTC/CAD"): "https://pro.kraken.com/app/trade/btc-cad",
    ("kraken", "ETH/CAD"): "https://pro.kraken.com/app/trade/eth-cad",
    ("kraken", "SOL/CAD"): "https://pro.kraken.com/app/trade/sol-cad",
    ("kraken", "BTC/USD"): "https://pro.kraken.com/app/trade/btc-usd",
    ("kraken", "ETH/USD"): "https://pro.kraken.com/app/trade/eth-usd",
}


def official_market_link(exchange, symbol):
    return VERIFIED_MARKET_LINKS.get((exchange, symbol))
