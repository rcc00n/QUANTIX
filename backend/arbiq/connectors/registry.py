"""Capability discovery and sourced eligibility; public access never approves trading."""

from datetime import UTC, datetime

import ccxt
import ccxt.async_support as async_ccxt

CSA_SOURCE = (
    "https://www.securities-administrators.ca/crypto-platforms-regulation-and-enforcement-actions/"
    "crypto-platforms-authorized-to-do-business-with-canadians/"
)
REGISTRATION_REVIEWED_AT = "2026-11-21"
REGISTERED_ENTITIES = {
    "kraken": "Payward Canada Inc.",
    "ndax": "Ndax Canada Inc.",
    "coinbase": "Coinbase Canada Inc.",
    "coinbaseexchange": "Coinbase Canada Inc.",
    "cryptocom": "Foris DAX CAN ULC",
}


def eligibility(exchange_id: str) -> dict:
    entity = REGISTERED_ENTITIES.get(exchange_id)
    return {
        "classification": "REGISTERED_ENTITY" if entity else "UNVERIFIED",
        "entity": entity,
        "manual_trading_eligible": False,
        "source": CSA_SOURCE if entity else None,
        "reviewed_at": REGISTRATION_REVIEWED_AT if entity else None,
        "scope": (
            "Canadian entity listed by CSA; province, account, assets and API product "
            "require review"
        )
        if entity
        else "No authoritative Canadian trading eligibility verified for this adapter",
        "product_verified": False,
        "venue_group": "coinbase" if exchange_id == "coinbaseexchange" else exchange_id,
    }


def available_candidates(requested: list[str], excluded: list[str]) -> tuple[list[str], list[str]]:
    selected = [name for name in requested if name not in excluded]
    return (
        [name for name in selected if name in async_ccxt.exchanges],
        [name for name in selected if name not in async_ccxt.exchanges],
    )


