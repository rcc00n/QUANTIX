"""Linear catalog indexing; comparisons are limited to selected compatible books."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations, islice

from arbiq.collector.config import CollectorConfig, MarketProfile
from arbiq.market_data.discovery import DiscoveredMarket, eligible_markets, selected_markets

CORE_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "LTC", "ADA", "LINK", "DOGE"})
CONVERSION_ASSETS = frozenset({"USD", "EUR", "USDC", "USDT"})
QUALITY_RANK = {
    "HEALTHY": 0,
    "DEGRADED": 2,
    "SUSPICIOUS": 3,
    "STALE": 4,
    "UNAVAILABLE": 5,
    "CLOCK_UNTRUSTED": 5,
}


def venue_group(slug: str) -> str:
    return "coinbase" if slug in {"coinbase", "coinbaseexchange"} else slug


def intersections(catalogs: dict[str, list[DiscoveredMarket]]) -> dict[tuple[str, str], set[str]]:
    coverage = defaultdict(set)
    for slug, markets in catalogs.items():
        for market in markets:
            if market.active is not False and market.base.verified and market.quote.verified:
                coverage[(market.base.key, market.quote.key)].add(venue_group(slug))
    return dict(coverage)


def allocate(
    catalogs: dict[str, list[DiscoveredMarket]],
    config: CollectorConfig,
    relevance: dict[tuple[str, str], int] | None = None,
    quality: dict[tuple[str, str], str] | None = None,
    *,
    research_epoch: int = 0,
    depth: Mapping[tuple[str, str], Decimal] | None = None,
):
    """Prefer intersections, retaining conversion anchors and configured filters.

    Liquid same-quote intersections outrank single-venue listings. Two CAD currency
    books, where present, anchor directional CAD conversion without stablecoin parity.
    All allocations respect the per-exchange cap; no unbounded subscriptions.
    """
    if config.market_profile is not None:
        return profile_allocation(
            catalogs, config, relevance or {}, quality or {}, depth or {}, research_epoch
        )

    coverage = intersections(catalogs)
    result = {}
    relevance = relevance or {}
    quality = quality or {}
    for slug, markets in catalogs.items():
        # Reuse Stage 2 filters without its small final cap.
        eligible = selected_markets(
            markets, config.model_copy(update={"max_markets_per_exchange": len(markets)})
        )

        def rank(m, slug=slug):
            count = len(coverage.get((m.base.key, m.quote.key), set()))
            return (
                0 if config.prefer_cad and m.quote.symbol == "CAD" else 1,
                0 if m.base.symbol in {"USD", "USDC", "USDT"} and m.quote.symbol == "CAD" else 1,
                0 if count > 1 else 1,
                config.priority_assets.index(m.base.symbol)
                if m.base.symbol in config.priority_assets
                else 999,
                -min(relevance.get((m.base.key, m.quote.key), 0), 10),
                {"HEALTHY": 0, "SUSPICIOUS": 1, "DEGRADED": 2, "STALE": 3, "UNAVAILABLE": 4}.get(
                    quality.get((slug, m.native_symbol)), 1
                ),
                -count,
                config.quote_assets.index(m.quote.symbol)
                if m.quote.symbol in config.quote_assets
                else 999,
                -(m.quote_volume or 0),
                m.native_symbol,
            )

        ordered = sorted(eligible, key=rank)
        result[slug] = ordered[: config.max_markets_per_exchange]
    return result


def spot_route_candidate(market: DiscoveredMarket) -> bool:
    return bool(
        market.active is not False
        and market.base.verified
        and market.quote.verified
        and (market.metadata.get("trading_rules") or {}).get("market_order_available") is not False
    )


def quote_liquidity_ranks(markets, native_values):
    """Rank within identical quote units and retain missing evidence as unknown."""
    by_quote = defaultdict(list)
    for market in markets:
        value = native_values(market)
        if value is not None and value > 0:
            by_quote[market.quote.key].append((value, market.native_symbol))
    # A normalized rank from 0 through 999 is dimensionless. A 1,000
    # sentinel means that no comparable measurement exists.
    ranks = {}
    for values in by_quote.values():
        ordered = sorted(values, key=lambda item: (-item[0], item[1]))
        previous = None
        rank = 0
        for position, (value, symbol) in enumerate(ordered):
            if position and value != previous:
                rank = position * 1000 // len(ordered)
            ranks[symbol] = rank
            previous = value
    return ranks


def profile_allocation(catalogs, config, relevance, quality, depth, research_epoch):
    eligible = {slug: eligible_markets(markets, config) for slug, markets in catalogs.items()}
    coverage = defaultdict(set)
    viable_symbols = {}
    for slug, markets in eligible.items():
        group = venue_group(slug)
        viable = set()
        for market in markets:
            if spot_route_candidate(market):
                viable.add(market.native_symbol)
                coverage[(market.base.key, market.quote.key)].add(group)
        viable_symbols[slug] = viable
    common_assets = CORE_ASSETS.union(config.priority_assets)
    quote_order = {value: index for index, value in enumerate(config.quote_assets)}
    cap = config.max_markets_per_exchange
    broad = config.market_profile == MarketProfile.BROAD_EXPLORATION
    liquid = config.market_profile == MarketProfile.LIQUID_MARKETS
    result = {}
    for slug, markets in eligible.items():
        if len(markets) <= cap:
            result[slug] = sorted(markets, key=lambda market: market.native_symbol)
            continue
        viable = viable_symbols[slug]
        liquidity = quote_liquidity_ranks(markets, lambda market: market.quote_volume)
        depth_rank = (
            quote_liquidity_ranks(
                markets, lambda market, slug=slug: depth.get((slug, market.native_symbol))
            )
            if depth
            else {}
        )

        def rank(market, slug=slug, depth_rank=depth_rank, liquidity=liquidity, viable=viable):
            key = (market.base.key, market.quote.key)
            groups = len(coverage.get(key, ()))
            observed_quality = QUALITY_RANK.get(quality.get((slug, market.native_symbol)), 1)
            liquid_rank = (
                depth_rank.get(market.native_symbol, 1000),
                liquidity.get(market.native_symbol, 1000),
            )
            base_prior = market.base.symbol not in common_assets
            identity = (
                market.native_symbol not in viable,
                groups < 2,
                observed_quality,
                market.active is not True,
                not bool(market.amount_step and market.min_notional),
            )
            cad_preferred = config.prefer_cad and market.quote.symbol != "CAD"
            observed_relevance = -min(3, max(0, relevance.get(key, 0)))
            profile_rank = (
                (*liquid_rank, base_prior, -groups, observed_relevance)
                if liquid
                else (base_prior, *liquid_rank, -groups, observed_relevance)
                if broad
                else (base_prior, -groups, *liquid_rank, observed_relevance)
            )
            return (
                *identity,
                *profile_rank,
                cad_preferred,
                quote_order.get(market.quote.symbol, len(quote_order)),
                market.native_symbol,
            )

        ordered = sorted(markets, key=rank)
        anchor_limit = 2 if cap >= 8 else 1
        chosen = list(
            islice(
                (
                    market
                    for market in ordered
                    if market.native_symbol in viable
                    and market.base.symbol in CONVERSION_ASSETS
                    and market.quote.symbol == "CAD"
                ),
                anchor_limit,
            )
        )
        anchor_symbols = {market.native_symbol for market in chosen}
        remaining = [market for market in ordered if market.native_symbol not in anchor_symbols]
        reserve = min(2 if broad else 1, max(0, (cap - 1) // 3))
        core_limit = cap - reserve
        base_limits = defaultdict(int)
        for market in chosen:
            base_limits[market.base.key] += 1
        for market in remaining:
            if len(chosen) >= core_limit:
                break
            if base_limits[market.base.key] < (2 if liquid else 1):
                chosen.append(market)
                base_limits[market.base.key] += 1

        # The rotation explores previously unchosen assets without letting recent
        # anomalies or an incumbent's measured depth dominate every slot.
        chosen_symbols = {market.native_symbol for market in chosen}
        if reserve:
            probes = {}
            for market in remaining:
                if market.base.key not in base_limits and market.native_symbol in viable:
                    probes.setdefault(market.base.key, market)
            bases = sorted(probes)
            for index in range(min(reserve, len(bases))):
                market = probes[bases[(research_epoch * reserve + index) % len(bases)]]
                chosen.append(market)
                chosen_symbols.add(market.native_symbol)
        # Venues with few independent bases still use their full book budget.
        chosen.extend(
            islice(
                (market for market in remaining if market.native_symbol not in chosen_symbols),
                cap - len(chosen),
            )
        )
        result[slug] = chosen
    return result


@dataclass(frozen=True)
class CoverageMetrics:
    monitored_markets: int
    compatible_intersections: int
    directed_comparisons: int
    catalog_intersections: int
    covered_keys: int


def metrics(catalogs, monitored) -> CoverageMetrics:
    discovered = intersections(catalogs)
    covered = intersections(monitored)
    pairs = sum(len(list(combinations(groups, 2))) for groups in covered.values())
    return CoverageMetrics(
        sum(len(x) for x in monitored.values()),
        pairs,
        pairs * 2,
        sum(len(v) > 1 for v in discovered.values()),
        sum(len(v) > 1 for v in covered.values()),
    )
