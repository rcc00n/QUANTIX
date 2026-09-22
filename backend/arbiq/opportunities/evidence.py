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


