"""Causal exact-depth sizing for an on-demand paper allocation, never a fill."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from arbiq.opportunities.analysis import load_input
from arbiq.opportunities.contracts import Costs, Evaluation, Fee, MarketInput
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
    if abs(time_value(books[0]["received_at"]) - time_value(books[1]["received_at"])) > timedelta(
        seconds=10
    ):
        raise ValueError("CROSS_BOOK_RECEIPT_SKEW")
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


