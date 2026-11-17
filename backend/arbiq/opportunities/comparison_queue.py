"""Incremental admission of bounded route work from latest market observations."""

from collections import OrderedDict, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from arbiq.opportunities.coverage import venue_group

Pair = tuple[UUID, UUID]


@dataclass(slots=True)
class _Sweep:
    pairs: Iterator[Pair]
    queued_at: float
    rerun_at: float | None = None


class ComparisonQueue:
    """Retain at most one in-progress sweep per selected book.

    A market update touches only its own comparisons. A new update during a
    sweep schedules another turn, including pairs already consumed. FIFO
    rotation across markets gives quieter assets a bounded place in line.
    """

    def __init__(self):
        self.ready: OrderedDict[Pair, float] = OrderedDict()
        self.deferred: OrderedDict[UUID, _Sweep] = OrderedDict()
        self.market_keys: dict[UUID, tuple[str, str, str]] = {}
        self.by_base: dict[str, list[UUID]] = {}
        self.by_quote: dict[str, list[UUID]] = {}

    def rebuild(self, market_map, index, now):
        refresh = bool(self.market_keys)
        active = {market_id for ids in index.values() for market_id in ids}
        self.market_keys.clear()
        by_base, by_quote = defaultdict(list), defaultdict(list)
        for market_id in sorted(active, key=str):
            runtime, market = market_map[market_id]
            key = market.base.key, market.quote.key, venue_group(runtime.slug)
            self.market_keys[market_id] = key
            by_base[key[0]].append(market_id)
            by_quote[key[1]].append(market_id)
        self.by_base, self.by_quote = dict(by_base), dict(by_quote)
        abandoned = len(self.ready) + len(self.deferred)
        self.ready.clear()
        self.deferred.clear()
        if refresh:
            self.submit(self.market_keys, now)
        return abandoned

    def _pairs(self, market_id):
        base, _, venue = self.market_keys[market_id]
        for other in self.by_base[base]:
            if self.market_keys[other][2] != venue:
                yield market_id, other
                yield other, market_id

    def submit(self, market_ids, now):
        coalesced = 0
        for market_id in market_ids:
            if market_id not in self.market_keys:
                continue
            work = self.deferred.get(market_id)
            if work:
                if work.rerun_at is None:
                    work.rerun_at = now
                coalesced += 1
            else:
                self.deferred[market_id] = _Sweep(self._pairs(market_id), now)
        return coalesced

    def admit(self, limit, attempt_budget, accepts):
        coalesced, examined, unavailable = 0, 0, 0
        while self.deferred and len(self.ready) < limit and examined < attempt_budget:
            market_id, work = self.deferred.popitem(last=False)
            pair = next(work.pairs, None)
            examined += 1
            if pair is None:
                if work.rerun_at is not None:
                    self.deferred[market_id] = _Sweep(self._pairs(market_id), work.rerun_at)
                continue
            self.deferred[market_id] = work
            if pair in self.ready:
                self.ready[pair] = min(self.ready[pair], work.queued_at)
                coalesced += 1
            elif accepts(pair):
                self.ready[pair] = work.queued_at
            else:
                unavailable += 1
        return examined, coalesced, unavailable
