"""Exact portfolio marks and no-trading benchmark, with missing-price propagation."""

from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select

from arbiq.db.models import Asset, Exchange
from arbiq.market_data.quality import safe_json
from arbiq.portfolio.accounting import lock_session, reconcile
from arbiq.portfolio.contracts import ACCOUNTING_VERSION, decimal_context, stored
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint, PortfolioPosition

ZERO = Decimal(0)


def complete_sum(values):
    values = list(values)
    if any(v is None for v in values):
        return None
    with localcontext() as ctx:
        ctx.prec = 78
        return stored(sum(values, ZERO))


def valid_price(price, now, maximum_age_seconds, *, cad=False):
    if cad:
        return Decimal(1), True, None
    if price is None or price.cad_per_unit is None:
        return None, False, "CAD_PRICE_UNAVAILABLE"
    if price.observed_at is None or price.received_at is None:
        return None, False, "VALUATION_TIMESTAMP_UNAVAILABLE"
    for timestamp in (price.observed_at, price.received_at):
        age = (now - timestamp).total_seconds()
        if age < 0:
            return None, False, "VALUATION_EVIDENCE_FROM_FUTURE"
        if age > maximum_age_seconds:
            return None, False, "VALUATION_EVIDENCE_STALE"
    return price.cad_per_unit, price.verified, None


def missing_status(reason, price):
    if reason == "VALUATION_EVIDENCE_STALE":
        return "STALE"
    if price and price.cad_per_unit is None:
        blockers = price.provenance.get("valuation_blockers", []) or [price.source]
        if blockers and all(
            item in {"valuation_book_stale", "cad_conversion_stale"} for item in blockers
        ):
            return "STALE"
    return "UNKNOWN"


@decimal_context
def inventory_imbalance(positions, benchmark_holdings):
    """Units required to restore initial venue shares, excluding transfer costs."""
    initial = {}
    current = {}
    for holding in benchmark_holdings:
        asset = str(holding["asset_id"])
        venue = str(holding["exchange_id"])
        group = initial.setdefault(asset, {})
        group[venue] = group.get(venue, ZERO) + Decimal(str(holding["quantity"]))
    for position in positions:
        asset = str(position["asset_id"])
        venue = str(position["exchange_id"])
        group = current.setdefault(asset, {})
        group[venue] = group.get(venue, ZERO) + position["quantity"]
    output = []
    with localcontext() as ctx:
        ctx.prec = 78
        for asset, venues in sorted(current.items()):
            total = sum(venues.values(), ZERO)
            target = initial.get(asset, {})
            initial_total = sum(target.values(), ZERO)
            shares = (
                {venue: quantity / initial_total for venue, quantity in target.items()}
                if initial_total
                else {}
            )
            required = (
                sum(
                    (
                        abs(venues.get(venue, ZERO) - total * shares.get(venue, ZERO))
                        for venue in set(venues) | set(shares)
                    ),
                    ZERO,
                )
                / 2
                if shares
                else None
            )
            output.append(
                {
                    "asset_id": asset,
                    "current_quantity": stored(total),
                    "current_by_exchange": venues,
                    "initial_target_shares": shares,
                    "quantity_to_rebalance": stored(required),
                    "imbalance_pct": stored(required / total * 100)
                    if required is not None and total > 0
                    else None,
                    "reason": None if shares else "NO_INITIAL_ALLOCATION_TARGET",
                    "costs_included": False,
                }
            )
    return output


