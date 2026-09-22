"""Bounded reuse of pure optimizer results, without reusing an observation."""

from collections import OrderedDict
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, getcontext

from arbiq.finance.contracts import utc_timestamp
from arbiq.market_data.contracts import NormalizedOrderBook
from arbiq.opportunities.config import EngineConfig
from arbiq.opportunities.contracts import (
    Costs,
    Fee,
    FXConversion,
    MarketInput,
    MarketRules,
    OptimizationResult,
)


def _levels_key(levels) -> tuple[tuple[str, str], ...]:
    return tuple((str(level.price), str(level.quantity)) for level in levels)


def _terms_key(value: Costs | Fee | MarketRules) -> tuple:
    """Keep Decimal representations, since equal values can have distinct exponents."""
    return tuple(
        str(item) if isinstance(item, Decimal) else item
        for item in (getattr(value, field.name) for field in fields(value))
    )


@dataclass(slots=True)
class _Revision:
    source: NormalizedOrderBook | FXConversion
    key: tuple
    token: object


class ProjectionCache:
    MAX_ROUTES = 512

    def __init__(self):
        self.depth: dict[str, _Revision] = {}
        self.fx: dict[str, _Revision] = {}
        self.results: OrderedDict[str, tuple[tuple, OptimizationResult]] = OrderedDict()

    def retain(self, market_ids) -> None:
        keys = {str(market_id) for market_id in market_ids}
        for market_id in self.depth.keys() - keys:
            del self.depth[market_id]

    def _depth_token(self, market_id: str, book: NormalizedOrderBook) -> object:
        previous = self.depth.get(market_id)
        if previous and previous.source is book:
            return previous.token
        key = _levels_key(book.bids), _levels_key(book.asks)
        token = previous.token if previous and previous.key == key else object()
        self.depth[market_id] = _Revision(book, key, token)
        return token

    def _fx_token(self, fx: FXConversion | None) -> object | None:
        if fx is None:
            return None
        previous = self.fx.get(fx.currency)
        if previous and previous.source is fx:
            return previous.token
        key = (
            fx.verified,
            *(
                tuple(
                    (
                        leg.source_currency,
                        leg.target_currency,
                        str(leg.fee_rate),
                        _levels_key(leg.levels),
                    )
                    for leg in route.legs
                )
                for route in (fx.to_cad, fx.from_cad)
            ),
        )
        token = previous.token if previous and previous.key == key else object()
        # The live graph has five currencies. Keep the bound for direct callers too.
        if fx.currency not in self.fx and len(self.fx) >= 5:
            self.fx.clear()
        self.fx[fx.currency] = _Revision(fx, key, token)
        return token

    def signature(
        self,
        buy: MarketInput,
        sell: MarketInput,
        buy_book: NormalizedOrderBook,
        sell_book: NormalizedOrderBook,
        config: EngineConfig,
        costs: Costs,
        now: datetime,
    ) -> tuple | None:
        utc_timestamp(now)
        # optimize_route can branch on UTC only at FX availability/expiry. Leave
        # anything outside the valid interval to the original, uncached path.
        if any(
            fx is not None
            and (
                fx.fresh_until < now
                or (fx.observed_at is not None and fx.observed_at > now)
                or (fx.available_at is not None and fx.available_at > now)
            )
            for fx in (buy.fx, sell.fx)
        ):
            return None
        context = getcontext()
        return (
            str(config.default_budget_cad),
            config.max_optimizer_evaluations,
            _terms_key(costs),
            (
                context.rounding,
                context.Emin,
                context.Emax,
                context.clamp,
                tuple(context.traps.values()),
            ),
            *(
                (
                    market.venue,
                    market.base_currency,
                    market.quote_currency,
                    _terms_key(market.rules),
                    _terms_key(market.fee),
                    self._depth_token(market.market_id, book),
                    self._fx_token(market.fx),
                )
                for market, book in ((buy, buy_book), (sell, sell_book))
            ),
        )

    def lookup(self, route: str, signature: tuple | None) -> OptimizationResult | None:
        previous = self.results.get(route)
        if signature is not None and previous and previous[0] == signature:
            self.results.move_to_end(route)
            return previous[1]
        return None

    def remember(
        self, route: str, signature: tuple | None, result: OptimizationResult, max_candidates: int
    ) -> None:
        if signature is not None:
            self.results[route] = (signature, result)
            self.results.move_to_end(route)
        while len(self.results) > min(max_candidates, self.MAX_ROUTES):
            self.results.popitem(last=False)
