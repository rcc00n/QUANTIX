"""Observed liquidation marks; missing depth/FX remains unavailable, never parity."""

from datetime import timedelta
from decimal import Decimal, localcontext

from sqlalchemy import select

from arbiq.db.models import Asset, Market
from arbiq.market_data.quality import DataState
from arbiq.opportunities.math import convert, fill_depth
from arbiq.portfolio.contracts import PriceEvidence

FX_ASSET_KEYS = {
    "fiat:CAD": "CAD",
    "fiat:EUR": "EUR",
    "fiat:USD": "USD",
    "asset:USDC": "USDC",
    "asset:USDT": "USDT",
}


def conversion_rejection(conversion, now):
    if conversion is None:
        return "cad_conversion_unavailable"
    if conversion.currency == "CAD":
        return None  # Identity does not consume a timestamped conversion book.
    if conversion.observed_at is None or conversion.available_at is None:
        return "cad_conversion_timestamp_unavailable"
    if conversion.observed_at > now or conversion.available_at > now:
        return "cad_conversion_future"
    if conversion.fresh_until < now:
        return "cad_conversion_stale"
    return None


