"""One bounded snapshot of existing live books for common paper opening inventory."""

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from hashlib import sha256
from itertools import combinations
from types import SimpleNamespace

import aiohttp
from fastapi import HTTPException
from sqlalchemy import select, tuple_

from arbiq.db.models import Asset, Exchange, Market
from arbiq.finance.fees import assess_fee
from arbiq.market_data.quality import CapturedBook
from arbiq.opportunities.analysis import dump_input
from arbiq.opportunities.contracts import Costs, Fee
from arbiq.opportunities.conversions import (
    FX_ASSET_KEYS,
    FXBook,
    capture_uncertainty_ms,
    select_cad_paths,
    spot_conversion_leg,
    synchronized_conversion,
)
from arbiq.opportunities.coverage import venue_group
from arbiq.opportunities.engine import ArbitrageEngine
from arbiq.paper_trading import allocation
from arbiq.paper_trading.api import now_checked
from arbiq.paper_trading.funding_sizing import (
    PinnedPair,
    checked_book,
    projected_costs,
    size_at_budget,
    time_value,
)
from arbiq.portfolio.contracts import SCALE, decimal_context, stored

MAX_BOOKS = 48
MAX_FX_BOOKS = 12
MAX_MARKETS_PER_ASSET = 6
MAX_PAIR_CANDIDATES = 96
PRIORITY = ("BTC", "ETH", "SOL", "XRP", "LTC", "ADA", "LINK", "DOGE")
TIERS = tuple(Decimal(size) for size in (50, 100, 250, 500))
FX_QUOTE_PRIORITY = ("CAD", "USDT", "USDC", "USD", "EUR")


@dataclass(frozen=True)
class SelectedMarket:
    market: Market
    exchange: Exchange
    base: Asset
    quote: Asset

    @property
    def venue(self):
        return venue_group(self.exchange.slug)

    @property
    def fx(self):
        return (
            self.base.canonical_key in FX_ASSET_KEYS and self.quote.canonical_key in FX_ASSET_KEYS
        )


async def cache_json(client, path):
    try:
        async with client.get(path) as response:
            return await response.json() if response.status == 200 else None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


def candidate_key(item, priority=FX_QUOTE_PRIORITY):
    return (
        priority.index(item.quote.symbol) if item.quote.symbol in priority else len(priority),
        item.venue,
        item.exchange.slug,
        item.market.native_symbol,
    )


def choose_markets(markets):
    """Bound concurrent GETs and give each supported base a deterministic slice."""
    fx = sorted((item for item in markets if item.fx), key=candidate_key)[:MAX_FX_BOOKS]
    by_asset = defaultdict(list)
    for item in sorted(markets, key=candidate_key):
        if not item.fx and item.quote.canonical_key in FX_ASSET_KEYS and item.base.kind == "crypto":
            by_asset[item.base.canonical_key].append(item)
    eligible = {}
    for key, rows in by_asset.items():
        best_by_venue = {}
        for row in rows:
            best_by_venue.setdefault(row.venue, row)
        if len(best_by_venue) >= 2:
            eligible[key] = list(best_by_venue.values())[:MAX_MARKETS_PER_ASSET]
    rank = {f"asset:{symbol}": i for i, symbol in enumerate(PRIORITY)}
    order = sorted(eligible, key=lambda key: (rank.get(key, len(rank)), key))
    chosen = []
    for position in range(MAX_MARKETS_PER_ASSET):
        for key in order:
            if len(chosen) >= MAX_BOOKS - len(fx):
                return fx + chosen
            if position < len(eligible[key]) and (
                position or len(chosen) + len(order) <= MAX_BOOKS
            ):
                chosen.append(eligible[key][position])
    return fx + chosen


async def monitored_markets(db, client):
    status = await cache_json(client, "/feeds/status")
    if not status or not status.get("healthy") or not status.get("clock", {}).get("trusted"):
        raise HTTPException(
            503, "Trusted live monitored universe unavailable for a common allocation"
        )
    selected = {
        (slug, symbol)
        for slug, feed in status["items"].items()
        if not feed.get("error_code")
        for symbol in feed["active_monitored_markets"]
    }
    if not selected or len(selected) > 600:
        raise HTTPException(422, "Current monitored universe is empty or outside the funding bound")
    markets = (
        await db.execute(
            select(Market, Exchange)
            .join(Exchange, Market.exchange_id == Exchange.id)
            .where(
                Market.active.is_(True),
                Exchange.enabled.is_(True),
                tuple_(Exchange.slug, Market.native_symbol).in_(sorted(selected)),
            )
        )
    ).all()
    asset_ids = {key for row, _ in markets for key in (row.base_asset_id, row.quote_asset_id)}
    assets = {
        asset.id: asset
        async for asset in await db.stream_scalars(select(Asset).where(Asset.id.in_(asset_ids)))
    }
    eligible = [
        SelectedMarket(
            market, exchange, assets[market.base_asset_id], assets[market.quote_asset_id]
        )
        for market, exchange in markets
        if all(
            assets[key].identifiers.get("identity_verified") is True
            for key in (market.base_asset_id, market.quote_asset_id)
        )
    ]
    return status, eligible


def confirmed_before(book, now, strategy):
    raw = book.model_dump(mode="json")
    if not checked_book(raw, now, timedelta(seconds=strategy.maximum_book_age_seconds), False):
        raise ValueError("BOOK_NOT_HEALTHY")
    uncertainty = timedelta(milliseconds=clock_uncertainty(book))
    if now - book.exchange_timestamp + uncertainty > timedelta(
        seconds=strategy.maximum_book_age_seconds
    ):
        raise ValueError("STALE_UNTRUSTED_OR_FUTURE_BOOK")
    return raw


def clock_uncertainty(book):
    return capture_uncertainty_ms(book.verification.get("clock") or {})


def market_fee(item, now, floor):
    meta = item.market.metadata_json
    return assess_fee(
        item.exchange.slug,
        item.base.symbol,
        item.quote.symbol,
        configured_taker_floor=floor,
        advertised_taker=meta.get("advertised_taker_rate"),
        now=now,
        fx_stablecoin=meta.get("fx_stablecoin") is True,
    )


def market_input(item, book, fx, *, fee, side, strategy):
    market = SimpleNamespace(
        id=item.market.id,
        base=SimpleNamespace(key=item.base.canonical_key),
        quote=item.quote,
        metadata=item.market.metadata_json,
        amount_step=item.market.amount_step,
        min_notional=item.market.min_notional,
        price_tick=item.market.price_tick,
    )
    value = ArbitrageEngine.market_input(
        SimpleNamespace(slug=item.exchange.slug), market, book, fx, fee=fee, side=side
    )
    rules = value.rules
    if (
        rules.amount_step is None
        or (rules.min_quantity is None and rules.min_notional is None)
        or rules.market_order_available is False
        or (rules.observed_at is not None and rules.observed_at > book.received_at)
    ):
        raise ValueError("NATIVE_TRADING_LIMITS_UNAVAILABLE")
    fee_currency = strategy.buy_fee_currency if side == "buy" else strategy.sell_fee_currency
    return replace(value, fee=Fee(max(value.fee.rate, strategy.taker_fee_rate), fee_currency))


def conversion_paths(rows, now, strategy, skew):
    edges = []
    for item, book, _raw, fee in rows:
        if item.fx:
            reference = str(item.market.id) + ":" + book.content_sha256
            params = {
                "bids": book.normalized.bids,
                "asks": book.normalized.asks,
                "reference": reference,
                "fee": fee,
            }
            edges.append(
                FXBook(
                    spot_conversion_leg(item.base.symbol, item.quote.symbol, **params, side="sell"),
                    spot_conversion_leg(item.base.symbol, item.quote.symbol, **params, side="buy"),
                    book.exchange_timestamp,
                    book.received_at,
                    clock_uncertainty(book),
                )
            )
    return select_cad_paths(
        edges,
        now=now,
        age=timedelta(seconds=strategy.maximum_book_age_seconds),
        skew=skew,
        probe_cad=Decimal(500),
    )


def pairs_by_asset(rows):
    by_asset = defaultdict(list)
    for entry in rows:
        item, *_ = entry
        if not item.fx:
            by_asset[item.base.canonical_key].append(entry)
    pairs = []
    for asset in sorted(by_asset):
        candidates = list(combinations(by_asset[asset], 2))
        candidates.sort(
            key=lambda pair: (
                pair[0][0].quote.id != pair[1][0].quote.id,
                candidate_key(pair[0][0]),
                candidate_key(pair[1][0]),
            )
        )
        if candidates:
            pairs.append(candidates)
    for position in range(max(map(len, pairs), default=0)):
        for candidates in pairs:
            if position < len(candidates):
                left, right = candidates[position]
                if left[0].venue != right[0].venue:
                    yield left, right
                    yield right, left


def prepare_pair(left, right, conversions, books, now, skew, strategy):
    buy, buy_book, buy_raw, buy_fee = left
    sell, sell_book, sell_raw, sell_fee = right
    uncertainty = timedelta(milliseconds=clock_uncertainty(buy_book) + clock_uncertainty(sell_book))
    if (
        abs(buy_book.received_at - sell_book.received_at) + uncertainty > skew
        or abs(buy_book.exchange_timestamp - sell_book.exchange_timestamp) + uncertainty > skew
    ):
        raise ValueError("CROSS_BOOK_SYNCHRONIZATION_UNAVAILABLE")
    for item, book in ((buy, buy_book), (sell, sell_book)):
        fx = conversions.get(item.quote.symbol)
        if fx is None:
            raise ValueError("CAD_CONVERSION_UNAVAILABLE")
        if not synchronized_conversion(
            fx, book.exchange_timestamp, book.received_at, clock_uncertainty(book), skew
        ):
            raise ValueError("FX_CAPTURE_SKEW_EXCEEDED")
    bi = market_input(buy, buy_book, conversions, fee=buy_fee, side="buy", strategy=strategy)
    si = market_input(sell, sell_book, conversions, fee=sell_fee, side="sell", strategy=strategy)
    refs = {
        leg.evidence.partition(":")[0]
        for market in (bi, si)
        for leg in (*market.fx.to_cad.legs, *market.fx.from_cad.legs)
    }
    all_evidence = (buy_raw, sell_raw, *(books[key] for key in sorted(refs)))
    route_id = "open:" + sha256(f"{buy.market.id}:{sell.market.id}".encode()).hexdigest()
    return route_id, PinnedPair(
        bi,
        si,
        now,
        Costs(slippage_bps=strategy.slippage_bps, rebalancing_bps=strategy.rebalance_bps),
        strategy.maximum_book_age_seconds,
        Decimal(1),
        {
            "source": "LIVE_COLLECTOR_DEPTH_COMMON_OPENING",
            "route_id": route_id,
            "base_asset": buy.base.canonical_key,
            "buy_market_id": str(buy.market.id),
            "sell_market_id": str(sell.market.id),
            "buy_book_sha256": buy_book.content_sha256,
            "sell_book_sha256": sell_book.content_sha256,
            "fx_market_ids": sorted(refs),
            "observed_at": min(
                time_value(book["exchange_timestamp"]) for book in all_evidence
            ).isoformat(),
            "received_at": min(
                time_value(book["received_at"]) for book in all_evidence
            ).isoformat(),
            "evaluated_at": now.isoformat(),
            "fees_verified": False,
            "account_eligibility_verified": False,
            "buy_fee": buy_fee.as_evidence("buy"),
            "sell_fee": sell_fee.as_evidence("sell"),
        },
    )


@decimal_context
def route_demand(route_id, pair, left, right, tier):
    sized = size_at_budget(pair, tier, at_least=tier < Decimal(500))
    if sized is None:
        return None
    acquisition = sized.acquisition_cad.quantize(SCALE, rounding=ROUND_DOWN)
    budget = max(tier, sized.acquisition_cad.quantize(SCALE, rounding=ROUND_CEILING))
    if not Decimal(50) <= acquisition <= Decimal(500) or sized.reserved_acquisition_cad > Decimal(
        500
    ):
        return None
    first, second = left[0], right[0]
    return allocation.RouteDemand(
        route_id,
        budget,
        acquisition,
        allocation.PricedAllocation(
            allocation.Cell(first.exchange.slug, first.quote.canonical_key),
            sized.buy_quote,
            sized.buy_mark_rate,
            pair.provenance,
        ),
        allocation.PricedAllocation(
            allocation.Cell(second.exchange.slug, second.base.canonical_key),
            sized.sell_base,
            sized.sell_mark_rate,
            pair.provenance,
        ),
        stored(sized.evaluation.net_profit_cad),
        evaluated_at=pair.at,
        projected_costs=projected_costs(sized.evaluation),
    )


@decimal_context
def opening_demands(selected, supplied, now, strategy, skew):
    excluded, books, rows = Counter(), {}, []
    for item, data in zip(selected, supplied, strict=True):
        try:
            book = CapturedBook.model_validate(data)
            if (
                book.market_id != item.market.id
                or book.exchange != item.exchange.slug
                or book.symbol != item.market.native_symbol
                or book.base_key != item.base.canonical_key
                or book.quote_key != item.quote.canonical_key
            ):
                raise ValueError("BOOK_IDENTITY_MISMATCH")
            raw = confirmed_before(book, now, strategy)
            fee = market_fee(item, now, strategy.taker_fee_rate)
        except (KeyError, TypeError, ValueError) as exc:
            reason = str(exc)
            excluded[
                reason if reason.isupper() and len(reason) < 80 else "LIVE_BOOK_UNAVAILABLE"
            ] += 1
            continue
        books[str(book.market_id)] = raw
        rows.append((item, book, raw, fee))
    conversions = conversion_paths(rows, now, strategy, skew)
    demands, evidence = [], {}
    for index, (left, right) in enumerate(pairs_by_asset(rows)):
        if index >= MAX_PAIR_CANDIDATES or len(evidence) >= allocation.MAX_ROUTES:
            break
        try:
            route_id, pair = prepare_pair(left, right, conversions, books, now, skew, strategy)
        except (ArithmeticError, TypeError, KeyError, ValueError) as exc:
            reason = str(exc)
            excluded[
                reason if reason.isupper() and len(reason) < 80 else "INVALID_DEPTH_EVIDENCE"
            ] += 1
            continue
        by_budget = {}
        for tier in TIERS:
            try:
                if (demand := route_demand(route_id, pair, left, right, tier)) is not None:
                    by_budget[demand.budget_cad] = demand
            except (ArithmeticError, ValueError):
                excluded["NO_EXECUTABLE_DEPTH_WITH_CAD_EVIDENCE"] += 1
        if not by_budget:
            excluded["NO_EXECUTABLE_DEPTH_WITH_CAD_EVIDENCE"] += 1
            continue
        demands.extend(by_budget.values())
        evidence[route_id] = {
            **pair.provenance,
            "calculation_inputs": {
                "buy": dump_input(pair.buy),
                "sell": dump_input(pair.sell),
                "costs": dump_input(pair.costs),
            },
        }
    return demands, evidence, books, dict(sorted(excluded.items()))


async def live_opening(request, db, strategy):
    client = request.app.state.collector_http
    if client is None:
        raise HTTPException(503, "Live collector unavailable for a common allocation")
    status, eligible = await monitored_markets(db, client)
    selected = choose_markets(eligible)
    supplied = []
    for offset in range(0, len(selected), 6):
        supplied.extend(
            await asyncio.gather(
                *(
                    cache_json(client, f"/books/{item.market.id}")
                    for item in selected[offset : offset + 6]
                )
            )
        )
    now = await now_checked(request)
    skew = timedelta(seconds=request.app.state.settings.arbitrage.maximum_book_skew_seconds)
    demands, evidence, books, exclusions = await asyncio.to_thread(
        opening_demands, selected, supplied, now, strategy, skew
    )
    return now, status["run_id"], eligible, selected, demands, evidence, books, exclusions
