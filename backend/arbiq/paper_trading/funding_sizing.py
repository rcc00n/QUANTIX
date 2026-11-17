"""Causal exact-depth sizing for an on-demand paper allocation, never a fill."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from arbiq.opportunities.analysis import load_input
from arbiq.opportunities.contracts import Costs, Evaluation, Fee, MarketInput
from arbiq.opportunities.conversions import capture_uncertainty_ms
from arbiq.opportunities.math import (
    conversion_capacity,
    convert,
    evaluate_route,
    fill_depth,
    maximum_quantity,
    quantity_for_notional,
    required_input,
)
from arbiq.paper_trading.execution import planned_reserves
from arbiq.portfolio.contracts import SCALE, decimal_context
from arbiq.settings.strategy import PaperStrategy

ONE = Decimal(1)
ZERO = Decimal(0)
TRADE_SIZES = tuple(Decimal(value) for value in (50, 100, 250, 500, 1000, 2500, 5000))


def time_value(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("EVIDENCE_TIMESTAMP_MISSING")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() != timedelta(0):
        raise ValueError("EVIDENCE_TIMESTAMP_NOT_UTC")
    return result


@dataclass(frozen=True, slots=True)
class PinnedPair:
    buy: MarketInput
    sell: MarketInput
    at: datetime
    costs: Costs
    maximum_age_seconds: int
    reliability: Decimal
    provenance: dict


@dataclass(frozen=True, slots=True)
class SizedPair:
    evaluation: Evaluation
    acquisition_cad: Decimal
    reserved_acquisition_cad: Decimal
    buy_quote: Decimal
    sell_base: Decimal
    buy_mark_rate: Decimal
    sell_mark_rate: Decimal


@dataclass(frozen=True, slots=True)
class InventoryBalance:
    quantity: Decimal = ZERO
    reserved: Decimal = ZERO

    @property
    @decimal_context
    def spendable(self) -> Decimal:
        return self.quantity - self.reserved


def projected_costs(evaluation: Evaluation) -> dict[str, str | None]:
    return {
        key: format(value, "f") if value is not None else None
        for key, value in {
            "fees_cad": evaluation.fees_cad,
            "fx_drag_cad": evaluation.fx_cost_cad,
            "slippage_reserve_cad": evaluation.slippage_reserve_cad,
            "rebalancing_cad": evaluation.rebalancing_cost_cad,
        }.items()
    }


def checked_book(book: dict, at: datetime, age: timedelta, allow_receipt_only: bool) -> bool:
    receipt = time_value(book["received_at"])
    source = time_value(book["exchange_timestamp"]) if book.get("exchange_timestamp") else None
    verification = book.get("verification", {})
    if book.get("state") not in {"HEALTHY", "DEGRADED"}:
        raise ValueError("BOOK_QUALITY_BLOCKS_ALLOCATION")
    if (
        not timedelta(0) <= at - receipt <= age
        or (source is not None and (source > receipt or not timedelta(0) <= at - source <= age))
        or verification.get("clock", {}).get("trusted") is False
        or verification.get("last_error_code")
    ):
        raise ValueError("STALE_UNTRUSTED_OR_FUTURE_BOOK")
    if source is None and not allow_receipt_only:
        raise ValueError("SOURCE_TIMESTAMP_MISSING")
    return source is not None and book["state"] == "HEALTHY"


def read_pair(evaluation, cutoff: datetime, strategy: PaperStrategy) -> PinnedPair:
    """Value depth at its own decision time and admit only records retained by cutoff."""
    evidence = evaluation.evidence
    inputs = evidence["calculation_inputs"]
    at = time_value(inputs["evaluated_at"])
    completed = evidence.get("timing", {}).get("evaluated_at")
    if (
        at > evaluation.evaluated_at
        or evaluation.evaluated_at > cutoff
        or evaluation.created_at > cutoff
        or (completed and time_value(completed) > cutoff)
        or not evidence.get("clock", {}).get("trusted")
    ):
        raise ValueError("EVALUATION_NOT_AVAILABLE_AT_CUTOFF")
    buy = load_input(inputs["buy"])
    sell = load_input(inputs["sell"])
    age = timedelta(seconds=strategy.maximum_book_age_seconds)
    books = (evidence["buy_book"], evidence["sell_book"])
    confidence = [
        checked_book(book, at, age, strategy.allow_missing_source_timestamp) for book in books
    ]
    uncertainty = timedelta(
        milliseconds=sum(
            capture_uncertainty_ms(book.get("verification", {}).get("clock") or {})
            for book in books
        )
    )
    max_skew = timedelta(seconds=10)
    if (
        abs(time_value(books[0]["received_at"]) - time_value(books[1]["received_at"])) + uncertainty
        > max_skew
    ):
        raise ValueError("CROSS_BOOK_RECEIPT_SKEW")
    if all(book.get("exchange_timestamp") for book in books) and (
        abs(time_value(books[0]["exchange_timestamp"]) - time_value(books[1]["exchange_timestamp"]))
        + uncertainty
        > max_skew
    ):
        raise ValueError("CROSS_BOOK_SOURCE_SKEW")
    fx_evidence = evidence.get("fx_books", {})
    used_fx = set()
    for item in (buy, sell):
        fx = item.fx
        if fx is None or fx.fresh_until < at:
            raise ValueError("CAD_CONVERSION_UNAVAILABLE")
        if item.quote_currency == "CAD":
            continue
        if (
            fx.observed_at is None
            or fx.available_at is None
            or not (
                timedelta(0) <= at - fx.available_at <= age
                and timedelta(0) <= at - fx.observed_at <= age
            )
        ):
            raise ValueError("FX_NOT_CAUSALLY_AVAILABLE")
        for leg in (*fx.to_cad.legs, *fx.from_cad.legs):
            identifier, separator, content = leg.evidence.partition(":")
            book = fx_evidence.get(identifier)
            if not separator or not book or content != book.get("content_sha256"):
                raise ValueError("FX_BOOK_PROVENANCE_MISSING")
            checked_book(book, at, age, False)
            used_fx.add(leg.evidence)
    buy = replace(
        buy, fee=Fee(max(buy.fee.rate, strategy.taker_fee_rate), strategy.buy_fee_currency)
    )
    sell = replace(
        sell, fee=Fee(max(sell.fee.rate, strategy.taker_fee_rate), strategy.sell_fee_currency)
    )
    mark_books = (*books, *(fx_evidence[key.partition(":")[0]] for key in sorted(used_fx)))
    mark_sources = [
        time_value(b["exchange_timestamp"]) for b in mark_books if b.get("exchange_timestamp")
    ]
    provenance = {
        "source": "PINNED_DEPTH_LIQUIDATION_CAD_MARK",
        "hypothetical_initial_inventory": True,
        "historical_projection": True,
        "evaluation_id": str(evaluation.id),
        "evaluated_at": at.isoformat(),
        "recorded_at": evaluation.created_at.isoformat(),
        "received_at": min(time_value(book["received_at"]) for book in mark_books).isoformat(),
        "observed_at": min(mark_sources).isoformat()
        if len(mark_sources) == len(mark_books)
        else None,
        "buy_book_sha256": books[0]["content_sha256"],
        "sell_book_sha256": books[1]["content_sha256"],
        "buy_snapshot_id": str(evaluation.buy_snapshot_id),
        "sell_snapshot_id": str(evaluation.sell_snapshot_id),
        "buy_fee_version_id": str(evaluation.buy_fee_version_id),
        "sell_fee_version_id": str(evaluation.sell_fee_version_id),
        "fx_book_references": sorted(used_fx),
        "source_timestamps_complete": all(bool(book.get("exchange_timestamp")) for book in books),
        "account_eligibility_verified": False,
        "fees_verified": False,
    }
    return PinnedPair(
        buy,
        sell,
        at,
        Costs(slippage_bps=strategy.slippage_bps, rebalancing_bps=strategy.rebalance_bps),
        strategy.maximum_book_age_seconds,
        ONE if all(confidence) else Decimal("0.5"),
        provenance,
    )


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@decimal_context
def price_size(pair: PinnedPair, quantity: Decimal, *, contribution_marks=True) -> SizedPair:
    evaluation = evaluate_route(pair.buy, pair.sell, quantity, now=pair.at, costs=pair.costs)
    cash, base = planned_reserves(evaluation, pair.buy.fee, pair.sell.fee, pair.costs.slippage_bps)
    buy_fx, sell_fx = pair.buy.fx, pair.sell.fx
    if buy_fx is None or sell_fx is None or evaluation.net_profit_cad is None:
        raise ValueError("CAD_CONVERSION_UNAVAILABLE")
    acquisition = evaluation.acquisition_cost_cad
    if acquisition is None:
        raise ValueError("CAD_CONVERSION_UNAVAILABLE")
    reserved = required_input(buy_fx.from_cad, cash)
    if not contribution_marks:
        return SizedPair(evaluation, acquisition, reserved, cash, base, ZERO, ZERO)
    # Assets enter at full-size liquidation value, independent from trade proceeds.
    buy_rate = (convert(buy_fx.to_cad, cash) / cash).quantize(SCALE, rounding=ROUND_DOWN)
    sell_rate = (convert(sell_fx.to_cad, fill_depth(pair.sell.bids, base)) / base).quantize(
        SCALE, rounding=ROUND_DOWN
    )
    if min(buy_rate, sell_rate) <= 0:
        raise ValueError("NON_STORABLE_MARK")
    return SizedPair(evaluation, acquisition, reserved, cash, base, buy_rate, sell_rate)


@decimal_context
def size_at_budget(
    pair: PinnedPair,
    budget: Decimal,
    *,
    at_least=False,
    buy_available=None,
    sell_available=None,
    contribution_marks=True,
) -> SizedPair | None:
    """Exact executable point at a budget boundary, bounded by both venue and FX depth.

    Proposed inventory can hold the first discrete point at or above a target, while
    a session executing under a hard budget uses the point at or below that cap.
    A smaller depth-limited point is returned with its true acquisition amount.
    """
    buy, sell = pair.buy, pair.sell
    if buy.fx is None or sell.fx is None or budget <= 0:
        return None
    step = buy.rules.amount_step or SCALE
    slippage = ONE + pair.costs.slippage_bps / 10000
    buy_factor = ONE + (buy.fee.rate if buy.fee.currency == "quote" else ZERO)
    acquired = ONE - (buy.fee.rate if buy.fee.currency == "base" else ZERO)
    sell_factor = ONE + (sell.fee.rate if sell.fee.currency == "base" else ZERO)
    buy_capacity = conversion_capacity(buy.fx.from_cad)
    budget_input = min(budget, buy_capacity) if buy_capacity is not None else budget
    quote = convert(buy.fx.from_cad, budget_input)
    if buy_available is not None:
        quote = min(quote, buy_available)
    if contribution_marks and (buy_liquidation := conversion_capacity(buy.fx.to_cad)) is not None:
        quote = min(quote, buy_liquidation)
    maximum = maximum_quantity(buy, sell)
    if sell_available is not None:
        sell_step = sell.rules.amount_step or SCALE
        sell_cap = floor_step(sell_available / sell_factor, sell_step)
        maximum = min(maximum, sell_cap * sell_factor / acquired)
    if (sell_conversion := conversion_capacity(sell.fx.to_cad)) is not None:
        proceeds_factor = ONE - (sell.fee.rate if sell.fee.currency == "quote" else ZERO)
        notional = sell_conversion if contribution_marks else sell_conversion / proceeds_factor
        size_limit = quantity_for_notional(sell.bids, notional)
        maximum = min(maximum, size_limit * (ONE if contribution_marks else sell_factor) / acquired)
    exact = quantity_for_notional(buy.asks, quote / (buy_factor * (ONE if at_least else slippage)))
    quantity = min(floor_step(maximum, step), (ceil_step if at_least else floor_step)(exact, step))
    upper = floor_step(maximum, step)
    candidates = (
        (quantity, min(upper, quantity + step)) if at_least else (quantity, quantity - step)
    )
    for point in dict.fromkeys(candidates):
        if point <= 0:
            continue
        try:
            result = price_size(pair, point, contribution_marks=contribution_marks)
        except (ValueError, ArithmeticError):
            continue
        if (buy_available is not None and result.buy_quote > buy_available) or (
            sell_available is not None and result.sell_base > sell_available
        ):
            continue
        if (
            at_least
            and result.acquisition_cad < budget
            and point < upper
            and point != candidates[-1]
        ):
            continue
        if at_least or result.reserved_acquisition_cad <= budget:
            return result
    return None


def prefunded_capacity(
    pair: PinnedPair,
    budget: Decimal,
    buy: InventoryBalance,
    sell: InventoryBalance,
) -> tuple[SizedPair | None, SizedPair | None]:
    market = size_at_budget(pair, budget, contribution_marks=False)
    if market is None:
        return None, None
    buy_available, sell_available = buy.spendable, sell.spendable
    if market.buy_quote <= buy_available and market.sell_base <= sell_available:
        return market, market
    return market, size_at_budget(
        pair,
        budget,
        buy_available=buy_available,
        sell_available=sell_available,
        contribution_marks=False,
    )


@decimal_context
def inventory_liquidation_mark(
    pair: PinnedPair, buy_quote: Decimal, sell_base: Decimal
) -> Decimal | None:
    """Value reserved inventory using pinned liquidation depth, before exit costs."""
    if pair.buy.fx is None or pair.sell.fx is None:
        return None
    try:
        quote_cad = convert(pair.buy.fx.to_cad, buy_quote) if buy_quote else ZERO
        base_cad = (
            convert(pair.sell.fx.to_cad, fill_depth(pair.sell.bids, sell_base))
            if sell_base
            else ZERO
        )
        return quote_cad + base_cad
    except (ValueError, ArithmeticError):
        return None


@decimal_context
def prefunded_net_return(size: SizedPair | None, inventory_mark: Decimal | None) -> Decimal | None:
    if size is None or inventory_mark is None or inventory_mark <= 0:
        return None
    net = size.evaluation.net_profit_cad
    return net * 100 / inventory_mark if net is not None else None
