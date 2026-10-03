"""Stage 8B planning cohort shared by comparison, proposals and offline replay."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from arbiq.opportunities.math import optimize_route
from arbiq.paper_trading.allocation import Cell, PricedAllocation, RouteDemand
from arbiq.paper_trading.funding_sizing import (
    TRADE_SIZES,
    PinnedPair,
    price_size,
    projected_costs,
    read_pair,
    size_at_budget,
)
from arbiq.portfolio.contracts import SCALE, decimal_context, stored
from arbiq.settings.strategy import PaperStrategy


@dataclass(frozen=True, slots=True)
class HistoricalRoute:
    route_id: str
    buy: Cell
    sell: Cell
    evaluation: Any
    frequency: int
    buy_exchange_id: str
    sell_exchange_id: str
    buy_asset_id: str
    sell_asset_id: str
    buy_asset_symbol: str
    sell_asset_symbol: str
    positive_frequency: int | None = None


@dataclass(slots=True)
class Cohort:
    demands: list[RouteDemand]
    legacy_requirements: list[dict]
    legacy_marks: dict
    requirements: list[dict]
    exclusions: Counter
    records: list[dict]


@decimal_context
def funding_demands(
    routes: list[HistoricalRoute],
    cutoff: datetime,
    strategy: PaperStrategy,
    *,
    include_legacy=False,
) -> Cohort:
    """Measure the same evidence at fixed tiers across every policy."""
    cohort = Cohort([], [], {}, [], Counter(), [])
    tiers = sorted(
        set(TRADE_SIZES) | {strategy.trade_amount_cad, strategy.min_buy_capital_cad} - {Decimal(0)}
    )
    for history in routes:
        evaluation = history.evaluation
        base = {
            "route_id": history.route_id,
            "evaluation_id": str(evaluation.id),
            "evaluated_at": evaluation.evaluated_at.isoformat(),
            "asset": history.sell_asset_symbol,
            "buy_exchange": history.buy.exchange,
            "sell_exchange": history.sell.exchange,
            "classification": "RESEARCH",
            "actionable": False,
        }
        try:
            pair = read_pair(evaluation, cutoff, strategy)
        except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError) as error:
            code = str(error)
            if (
                not code
                or len(code) > 64
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for c in code)
            ):
                code = "INVALID_OR_INCOMPLETE_PINNED_EVIDENCE"
            cohort.exclusions[code] += 1
            cohort.requirements.append({**base, "required": [], "funded": False, "reasons": [code]})
            continue
        sizes = []
        for tier in tiers:
            try:
                sizes.append((tier, size_at_budget(pair, tier, at_least=True)))
            except (TypeError, ValueError, ArithmeticError):
                cohort.exclusions["MISSING_EXECUTABLE_SIZE_EVIDENCE"] += 1
        valid = [size for _, size in sizes if size is not None]
        cohort.records.append(
            {
                **base,
                **pair.provenance,
                "frequency": history.frequency,
                "retained_positive_count": history.positive_frequency,
            }
        )
        largest = max((v.acquisition_cad for v in valid), default=Decimal(0)).quantize(
            SCALE, rounding=ROUND_DOWN
        )
        seen_budgets = set()
        for tier, size in sizes:
            if size is None:
                continue
            acquisition = size.acquisition_cad.quantize(SCALE, rounding=ROUND_DOWN)
            cap = max(tier, size.acquisition_cad.quantize(SCALE, rounding=ROUND_CEILING))
            if acquisition <= 0 or cap in seen_budgets:
                continue
            seen_budgets.add(cap)
            try:
                cohort.demands.append(
                    RouteDemand(
                        history.route_id,
                        cap,
                        acquisition,
                        PricedAllocation(
                            history.buy, size.buy_quote, size.buy_mark_rate, pair.provenance
                        ),
                        PricedAllocation(
                            history.sell, size.sell_base, size.sell_mark_rate, pair.provenance
                        ),
                        stored(size.evaluation.net_profit_cad),
                        history.frequency,
                        largest,
                        pair.reliability,
                        str(evaluation.id),
                        pair.at,
                        projected_costs=projected_costs(size.evaluation),
                    )
                )
            except ValueError:
                cohort.exclusions["UNREPRESENTABLE_ALLOCATION"] += 1
        target = next((v for t, v in sizes if t == strategy.trade_amount_cad), None)
        sized = target or max(valid, key=lambda v: v.acquisition_cad, default=None)

        def details(size, history=history):
            return [
                {
                    "exchange_id": eid,
                    "exchange": cell.exchange,
                    "asset_id": aid,
                    "asset": symbol,
                    "canonical_key": cell.asset,
                    "required_quantity": format(q, "f"),
                }
                for eid, aid, symbol, cell, q in (
                    (
                        history.buy_exchange_id,
                        history.buy_asset_id,
                        history.buy_asset_symbol,
                        history.buy,
                        size.buy_quote,
                    ),
                    (
                        history.sell_exchange_id,
                        history.sell_asset_id,
                        history.sell_asset_symbol,
                        history.sell,
                        size.sell_base,
                    ),
                )
            ]

        warning = [] if sized is not None else ["NO_EXECUTABLE_DEPTH_WITH_CAD_EVIDENCE"]
        if sized is not None and sized.acquisition_cad < strategy.trade_amount_cad:
            warning.append("TARGET_ACQUISITION_NOT_OBSERVED")
        cohort.requirements.append(
            {
                **base,
                "required": details(sized) if sized else [],
                "funded": False,
                "reasons": warning,
                "projected_net_profit_cad": str(sized.evaluation.net_profit_cad) if sized else None,
                "actual_projected_acquisition_cad": str(sized.acquisition_cad) if sized else None,
            }
        )
        if include_legacy:
            _legacy_point(cohort, pair, history, base, strategy, details)
    return cohort


