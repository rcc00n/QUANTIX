"""Radar classification and ordering over captured calculation evidence."""

from decimal import Decimal, InvalidOperation
from math import isfinite

CONFIDENCE = {"HEALTHY": 0, "DEGRADED": 1, "SUSPICIOUS": 2, "STALE": 3, "UNAVAILABLE": 4}
FRESHNESS = {"CURRENT": 0, "RETAINED": 1, "UNVERIFIED": 2, "STALE": 3}


def financial_amount(raw) -> Decimal | None:
    try:
        value = Decimal(raw) if isinstance(raw, (str, Decimal)) else None
        return value if value is not None and value.is_finite() else None
    except InvalidOperation:
        return None


def _positive(raw) -> bool | None:
    amount = financial_amount(raw)
    return amount > 0 if amount is not None else None


def _duration(raw) -> float | None:
    return raw if type(raw) in (float, int) and isfinite(raw) and raw >= 0 else None


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _both(results: list[bool | None]) -> bool | None:
    if False in results:
        return False
    return None if None in results else True


def _synchronized(timing: dict, name: str, skew_limit: int | None) -> bool | None:
    if timing.get("clock_trusted") is not True or skew_limit is None:
        return None
    seconds = _duration(timing.get(name))
    uncertainty = _duration(timing.get("cross_book_uncertainty_seconds"))
    if seconds is None or uncertainty is None:
        return None
    return seconds + uncertainty <= skew_limit


def classify_route(
    payload: dict,
    evidence: dict,
    *,
    freshness: str,
    maximum_book_skew: int | None,
) -> dict:
    reasons = set(payload.get("reasons", ()))
    missing_size = bool(
        reasons & {"BUDGET_EVIDENCE_UNAVAILABLE", "CALCULATION_EVIDENCE_UNAVAILABLE"}
    )
    costs = {} if missing_size else _object(payload.get("costs"))
    required = [
        _positive(costs.get(field))
        for field in ("buy_quantity", "sell_quantity", "buy_quote_required", "sell_base_required")
    ]
    if (
        reasons & {"NO_FEASIBLE_EXECUTABLE_SIZE", "SELECTED_BUDGET_HAS_NO_FEASIBLE_SIZE"}
        and not missing_size
        and not any(result is True for result in required)
    ):
        depth_feasible = False
    else:
        depth_feasible = _both(required)
    net_positive = _positive(payload.get("net_profit_cad"))
    assumptions = _object(evidence.get("fee_assumptions"))
    inputs = _object(evidence.get("calculation_inputs"))
    fees = {}
    fee_verified = []
    for side in ("buy", "sell"):
        assumption = _object(assumptions.get(side))
        fees[side] = assumption.get("fee_evidence_status")
        account = assumption.get("account_tier_verified")
        if account is False or assumption.get("fee_currency_assumed") is True:
            fee_verified.append(False)
        elif (
            account is True
            and fees[side] == "ACCOUNT_VERIFIED"
            and assumption.get("fee_currency_assumed") is False
        ):
            fee_verified.append(True)
        else:
            legacy_terms = _object(_object(inputs.get(side)).get("fee"))
            fee_verified.append(False if legacy_terms.get("verified") is False else None)
    timing = _object(evidence.get("timing"))
    raw_gap = payload.get("raw_price_discrepancy")
    return {
        "evidence_basis": payload["storage_origin"],
        "freshness": freshness,
        "raw_price_discrepancy": raw_gap if type(raw_gap) is bool else None,
        "theoretically_positive": _positive(costs.get("gross_profit_cad")),
        "depth_feasible": depth_feasible,
        "depth_executable_positive": _both([depth_feasible, net_positive]),
        "inventory_funded": None,
        "fee_verified": _both(fee_verified),
        "fee_evidence": fees,
        "account_eligible": None,
        "actionable": False,
        "receipts_within_skew_limit": _synchronized(
            timing, "cross_book_receive_skew_seconds", maximum_book_skew
        ),
        "sources_within_skew_limit": _synchronized(
            timing, "cross_book_source_skew_seconds", maximum_book_skew
        ),
    }


def _descending_financial(value, field):
    number = financial_amount(value.get(field))
    return number is None, number.copy_negate() if number is not None else Decimal(0)


def financial_sort(value: dict, field: str) -> tuple:
    """Sort money exactly, then prefer stronger evidence when money ties."""
    classifications = value.get("classifications") or {}
    truth_rank = {True: 0, None: 1, False: 2}
    age = _duration(value.get("evidence_age_seconds"))
    return (
        *_descending_financial(value, field),
        CONFIDENCE.get(value.get("data_confidence"), len(CONFIDENCE)),
        FRESHNESS.get(classifications.get("freshness"), len(FRESHNESS)),
        truth_rank[classifications.get("sources_within_skew_limit")],
        truth_rank[classifications.get("receipts_within_skew_limit")],
        *_descending_financial(value, "maximum_liquidity_cad"),
        *_descending_financial(value, "net_return_pct"),
        age is None,
        age or 0,
        truth_rank[classifications.get("account_eligible")],
        truth_rank[classifications.get("fee_verified")],
        value["id"],
    )
