"""Prioritize transport without changing the monitored market universe."""

from decimal import Decimal

from arbiq.opportunities.coverage import intersections, quote_liquidity_ranks, spot_route_candidate


def native_symbols(
    slug, monitored, priorities, limit, *, profile=None, quality=None, depth=None, retained=None
):
    """Reserve a few liquid-position slots and spread priority across venues."""
    coverage = intersections(
        {
            venue: [market for market in markets if spot_route_candidate(market)]
            for venue, markets in monitored.items()
        }
        if profile
        else monitored
    )
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
        and (not profile or spot_route_candidate(market))
        and market.base.symbol in rank
        and len(coverage.get((market.base.key, market.quote.key), ())) > 1
    ]

    def legacy_order(market):
        return (
            rank[market.base.symbol],
            -len(coverage[(market.base.key, market.quote.key)]),
            # Volumes in different quote currencies are not directly comparable.
            market.quote.symbol,
            -(market.quote_volume or 0),
            market.native_id,
        )

    if profile:
        primary = set(order[:limit])
        quality = quality or {}
        depth = depth or {}
        retained = retained or {}
        measured = quote_liquidity_ranks(
            eligible, lambda market: depth.get((slug, market.native_symbol))
        )

        def compatible_order(market):
            state = quality.get((slug, market.native_symbol))
            previous = retained.get(market.native_id)
            return (
                # A quiet book can keep a synchronized subscription. Quote age
                # alone gives no evidence that its transport should be replaced.
                state in {"SUSPICIOUS", "UNAVAILABLE", "CLOCK_UNTRUSTED"},
                not (previous and previous.id == market.id),
                market.base.symbol not in primary,
                -len(coverage[(market.base.key, market.quote.key)]),
                measured.get(market.native_symbol, 1000),
                *legacy_order(market),
            )

        eligible.sort(key=compatible_order)
    else:
        eligible.sort(key=legacy_order)
    selected = {}
    seen_bases = set()
    for market in eligible:
        if len(selected) >= limit:
            break
        if market.base.key not in seen_bases and market.native_id not in selected:
            selected[market.native_id] = market
            seen_bases.add(market.base.key)
    return selected


def tier(market, priorities, fast_ids):
    if market.id in fast_ids:
        return "FAST_LANE"
    if market.base.symbol in priorities or (
        market.active is True
        and market.quote_volume is not None
        and market.quote_volume >= Decimal("1000000")
    ):
        return "STANDARD"
    return "RESEARCH"
