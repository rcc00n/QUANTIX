"""Prioritize transport without changing the monitored market universe."""

from decimal import Decimal

from arbiq.opportunities.coverage import intersections


def native_symbols(slug, monitored, priorities, limit):
    """Reserve a few liquid-position slots and spread priority across venues."""
    coverage = intersections(monitored)
    order = list(priorities)
    if slug == "okx" and len(order) >= 4:
        # Give the second venue first chance at the next two priority assets.
        order[:4] = [*order[2:4], *order[:2]]
    rank = {symbol: index for index, symbol in enumerate(order)}
    eligible = [
        market
        for market in monitored.get(slug, ())
        if market.id is not None
        and market.active is True
        and market.base.verified
        and market.quote.verified
        and market.base.symbol in rank
        and len(coverage.get((market.base.key, market.quote.key), ())) > 1
    ]
    eligible.sort(
        key=lambda market: (
            rank[market.base.symbol],
            -len(coverage[(market.base.key, market.quote.key)]),
            # Volumes in different quote currencies are not directly comparable.
            market.quote.symbol,
            -(market.quote_volume or 0),
            market.native_id,
        )
    )
    selected = {}
    seen_bases = set()
    for market in eligible:
        if len(selected) >= limit:
            break
        if market.base.key not in seen_bases and market.native_id not in selected:
            selected[market.native_id] = market
            seen_bases.add(market.base.key)
    return selected


