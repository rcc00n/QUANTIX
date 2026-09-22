"""Bounded sharing of immutable serialized evidence, never evidence removal.

A book/input is reused only when the original immutable source or complete typed
input is unchanged. Current routes retain their exact historical JSON tree even
when a newer pool entry replaces it. SQL persistence still writes full evidence.
"""

from collections import OrderedDict
from typing import Any

from arbiq.market_data.quality import CapturedBook, safe_json
from arbiq.opportunities.analysis import dump_input
from arbiq.opportunities.contracts import MarketInput


class FrozenJSON(dict):
    """JSON-serializable mapping whose shared values cannot be changed in place."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("Shared financial evidence is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def freeze_json(value: Any):
    if isinstance(value, FrozenJSON):
        return value
    if isinstance(value, dict):
        return FrozenJSON({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return value


def shared_json(value: Any):
    """Normalize scalars while reusing only already-frozen financial subtrees."""
    if isinstance(value, FrozenJSON):
        return value
    if isinstance(value, dict):
        return {str(key): shared_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [shared_json(item) for item in value]
    return safe_json(value)


class EvidencePool:
    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError("Evidence pool requires a positive bound")
        self.limit = limit
        self.books: OrderedDict[str, tuple[CapturedBook, FrozenJSON]] = OrderedDict()
        self.inputs: OrderedDict[str, tuple[MarketInput, FrozenJSON]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def _remember(self, cache, key, source, payload):
        cache[key] = (source, payload)
        cache.move_to_end(key)
        while len(cache) > self.limit:
            cache.popitem(last=False)
        self.misses += 1
        return payload

    def book(self, value: CapturedBook) -> FrozenJSON:
        key = str(value.market_id)
        previous = self.books.get(key)
        if previous and previous[0] is value:
            self.hits += 1
            self.books.move_to_end(key)
            return previous[1]
        return self._remember(
            self.books, key, value, freeze_json(safe_json(value.model_dump(mode="json")))
        )

    def market_input(self, value: MarketInput) -> FrozenJSON:
        previous = self.inputs.get(value.market_id)
        if previous and previous[0] == value:
            self.hits += 1
            self.inputs.move_to_end(value.market_id)
            return previous[1]
        return self._remember(self.inputs, value.market_id, value, freeze_json(dump_input(value)))

    def retain(self, market_ids):
        keys = {str(value) for value in market_ids}
        for cache in (self.books, self.inputs):
            for key in list(cache):
                if key not in keys:
                    cache.pop(key)

    def current(self, body: dict, payload: dict) -> dict:
        """Attach already-normalized immutable trees after ordinary scalar dumping.

        The resulting JSON is byte-equivalent under canonical serialization to a
        full independent safe_json copy. Only Python object ownership changes.
        """
        original = payload.get("evidence", {})
        evidence = body.get("evidence")
        if not isinstance(evidence, dict):
            return body
        if "fx_books" in original:
            evidence["fx_books"] = freeze_json(original["fx_books"])
        inputs = original.get("calculation_inputs", {})
        if isinstance(evidence.get("calculation_inputs"), dict):
            for side in ("buy", "sell"):
                if isinstance(inputs.get(side), FrozenJSON):
                    evidence["calculation_inputs"][side] = inputs[side]
        return body

    def metrics(self):
        return {
            "book_entries": len(self.books),
            "input_entries": len(self.inputs),
            "entry_limit": self.limit,
            "hits": self.hits,
            "misses": self.misses,
        }
