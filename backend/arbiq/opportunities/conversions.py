"""Causal and bounded selection of executable CAD conversion evidence."""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from arbiq.market_data.identity import FIAT
from arbiq.opportunities.contracts import (
    ConversionLeg,
    ConversionLevel,
    ConversionRoute,
    FXConversion,
    identity_cad,
)
from arbiq.opportunities.math import (
    InsufficientDepth,
    conversion_capacity,
    conversion_leg_from_book,
    convert,
    required_input,
)

# Alternative paths are selected before sizing, so one route retains a concave cost curve.
FX_ASSET_KEYS = {
    **{f"fiat:{symbol}": symbol for symbol in FIAT},
    "asset:USDC": "USDC",
    "asset:USDT": "USDT",
}
FX_PATH_VERSION = "cad-depth-path-v2"
_MAX_HOPS = 3
_MAX_EXPANSIONS = 128
_MAX_PARALLEL_BOOKS = 3


def capture_uncertainty_ms(clock: dict) -> int:
    """Keep the existing conservative two-second fallback for absent capture bounds."""
    value = clock.get("uncertainty_ms")
    if type(value) in (int, float) and 0 <= value <= 2000:
        return int(value) + (value > int(value))
    return 2000


@dataclass(frozen=True, slots=True)
class FXBook:
    sell: ConversionLeg
    buy: ConversionLeg
    observed_at: datetime
    received_at: datetime
    uncertainty_ms: int


class ConversionSnapshot(dict[str, FXConversion]):
    """Keep a batch's depth evidence attached across asynchronous evaluations."""

    def __init__(self, routes, books):
        super().__init__(routes)
        self.books = books

    def referenced_books(self, *conversions):
        references = {
            leg.evidence
            for conversion in conversions
            if conversion is not None
            for leg in (*conversion.to_cad.legs, *conversion.from_cad.legs)
        }
        return {
            key: self.books[key]
            for reference in sorted(references)
            if (key := reference.split(":", 1)[0]) in self.books
        }


def spot_conversion_leg(base, quote, *, bids, asks, reference, fee, side):
    reverse = side == "buy"
    source_fee = fee.currency(side) == ("quote" if reverse else "base")
    rate = fee.applied_taker_rate
    leg = conversion_leg_from_book(
        base,
        quote,
        bids=bids,
        asks=asks,
        reverse=reverse,
        evidence=reference,
        fee_rate=Decimal(0) if source_fee else rate,
    )
    if not source_fee:
        return leg
    # Include the source-denominated commission in source depth and price units.
    with localcontext() as ctx:
        ctx.prec = 78
        factor = 1 + rate
        return ConversionLeg(
            leg.source_currency,
            leg.target_currency,
            tuple(ConversionLevel(x.price / factor, x.quantity * factor) for x in leg.levels),
            evidence=leg.evidence,
        )


def _paired_capacity(liquidation, acquisition):
    cad_capacity = conversion_capacity(acquisition)
    try:
        liquidation_cap = required_input(acquisition, conversion_capacity(liquidation))
    except InsufficientDepth:
        return cad_capacity
    return min(cad_capacity, liquidation_cap)


def _coherent(books, skew):
    if len(books) == 1:
        return True
    uncertainties = sorted((edge.uncertainty_ms for edge in books), reverse=True)
    margin = timedelta(milliseconds=uncertainties[0] + uncertainties[1])
    for stamps in (
        tuple(edge.observed_at for edge in books),
        tuple(edge.received_at for edge in books),
    ):
        if max(stamps) - min(stamps) + margin > skew:
            return False
    return True


def select_cad_paths(edges, *, now, age, skew, probe_cad):
    """Use direction-specific depth on the same synchronized set of spot books.

    Among paths supporting the same CAD probe, prefer shorter paths, then the
    stronger bid/ask round trip. Thin paths compete on supported size first.
    No synthetic peg, quantity extrapolation, or account fee verification.
    """
    pairs = defaultdict(list)
    for edge in edges:
        edge_age = max(now - edge.observed_at, now - edge.received_at)
        if not edge.observed_at <= edge.received_at <= now:
            continue
        if edge_age + timedelta(milliseconds=edge.uncertainty_ms) > age:
            continue
        pair = tuple(sorted((edge.sell.source_currency, edge.sell.target_currency)))
        pairs[pair].append(edge)
    graph = defaultdict(list)
    with localcontext() as ctx:
        ctx.prec = 78
        for (left, _right), books in sorted(pairs.items()):

            def priority(edge, left=left):
                first, other = (
                    (edge.sell, edge.buy)
                    if edge.sell.source_currency == left
                    else (edge.buy, edge.sell)
                )
                # Both contributions are in units of the same currency (left).
                depth = min(
                    sum(level.quantity for level in first.levels),
                    sum(level.quantity * level.price for level in other.levels)
                    * (1 - other.fee_rate),
                )
                return -depth, now - edge.observed_at, edge.sell.evidence

            for edge in sorted(books, key=priority)[:_MAX_PARALLEL_BOOKS]:
                for leg, inverse in ((edge.sell, edge.buy), (edge.buy, edge.sell)):
                    graph[leg.source_currency].append((leg, inverse, edge))
        results = {"CAD": identity_cad()}
        for currency in sorted(graph.keys() - {"CAD"}):
            best = None
            first = (currency, (), (), (), frozenset({currency}))
            queue = deque([first])
            expansions = 0
            while queue and expansions < _MAX_EXPANSIONS:
                node, outward, inward, books, visited = queue.popleft()
                for leg, inverse, edge in graph.get(node, ()):
                    if expansions >= _MAX_EXPANSIONS:
                        break
                    expansions += 1
                    target = leg.target_currency
                    if target in visited:
                        continue
                    path = (*books, edge)
                    if not _coherent(path, skew):
                        continue
                    out, back = (*outward, leg), (inverse, *inward)
                    if target != "CAD":
                        if len(path) < _MAX_HOPS:
                            queue.append((target, out, back, path, visited | {target}))
                        continue
                    liquidation, acquisition = ConversionRoute(out), ConversionRoute(back)
                    try:
                        capacity = _paired_capacity(liquidation, acquisition)
                        probe = min(capacity, max(probe_cad, Decimal(1)))
                        # Step one working-precision unit inside an exact depth boundary.
                        trial = probe.next_minus()
                        proceeds = convert(liquidation, convert(acquisition, trial))
                    except (ValueError, ArithmeticError):
                        continue
                    rank = (probe, -len(path), proceeds, min(x.observed_at for x in path))
                    if best is None or rank > best[0]:
                        best = rank, liquidation, acquisition, path
            if best is None:
                continue
            _, liquidation, acquisition, path = best
            results[currency] = FXConversion(
                currency,
                liquidation,
                acquisition,
                False,
                min(
                    edge.observed_at + age - timedelta(milliseconds=edge.uncertainty_ms)
                    for edge in path
                ),
                "Executable spot conversion depth; account fees and funding unverified",
                observed_at=max(edge.observed_at for edge in path),
                available_at=max(edge.received_at for edge in path),
                oldest_observed_at=min(edge.observed_at for edge in path),
                oldest_available_at=min(edge.received_at for edge in path),
                capture_uncertainty_ms=max(edge.uncertainty_ms for edge in path),
                selection_version=FX_PATH_VERSION,
            )
    return results


def synchronized_conversion(conversion, observed_at, received_at, uncertainty_ms, skew):
    """Check a new conversion's entire causal interval against its traded book."""
    if conversion is None or conversion.selection_version is None:
        return True  # Historical pinned contracts retain their original interpretation.
    if observed_at is None:
        return False
    margin = timedelta(milliseconds=uncertainty_ms + conversion.capture_uncertainty_ms)
    for own, oldest, latest in (
        (observed_at, conversion.oldest_observed_at, conversion.observed_at),
        (received_at, conversion.oldest_available_at, conversion.available_at),
    ):
        if max(own, latest) - min(own, oldest) + margin > skew:
            return False
    return True
