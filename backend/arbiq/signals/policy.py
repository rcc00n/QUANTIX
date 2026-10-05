"""Deterministic independent alert gates over immutable observed evaluations."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

EDMONTON = ZoneInfo("America/Edmonton")


def timestamp(value):
    if isinstance(value, datetime):
        return value
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def amount(value):
    if isinstance(value, (bool, float)) or value is None:
        return None
    try:
        value = Decimal(value)
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def quiet_hours(now, policy):
    if not policy.quiet_hours_enabled or policy.quiet_start == policy.quiet_end:
        return False
    local_time = now.astimezone(EDMONTON).strftime("%H:%M")
    if policy.quiet_start < policy.quiet_end:
        return policy.quiet_start <= local_time < policy.quiet_end
    return local_time >= policy.quiet_start or local_time < policy.quiet_end


@dataclass(frozen=True)
class Gate:
    eligible: bool
    classification: str
    emergency: bool
    silent: bool
    reasons: tuple[str, ...]


def evaluate(result, evidence, policy, now, *, clock_trusted, persistence_seconds=0):
    """Every financial threshold is AND. Research never qualifies for escalation."""
    reasons = []
    actionable = result.get("actionable") is True
    classification = "ACTIONABLE" if actionable else "RESEARCH_ONLY"
    expiry = timestamp(result.get("expires_at"))
    if expiry is None or now >= expiry:
        reasons.append("OPPORTUNITY_EXPIRED")
    if result.get("status") not in {"VERIFIED_MARKET", "RESEARCH_ONLY"}:
        reasons.append("OPPORTUNITY_NOT_CONFIRMED")
    if not clock_trusted:
        reasons.append("UTC_CLOCK_UNTRUSTED")
    supporting = [
        timestamp(result.get("supporting_received_at")),
        timestamp(result.get("supporting_source_at")),
    ]
    ages = [(now - t).total_seconds() for t in supporting if t]
    source_clock = evidence.get("clock", {})
    uncertainty = amount(source_clock.get("uncertainty_seconds"))
    if uncertainty is None:
        milliseconds = amount(source_clock.get("uncertainty_ms"))
        uncertainty = milliseconds / Decimal("1000") if milliseconds is not None else None
    if uncertainty is not None and not Decimal("0") <= uncertainty <= Decimal("2"):
        reasons.append("SOURCE_CLOCK_UNCERTAINTY_INVALID")
        uncertainty = Decimal("2")
    if uncertainty is None:
        uncertainty = Decimal("2")  # Strict worst-case worker clock bound, never zero.
    if len(ages) != 2 or any(a < 0 for a in ages):
        reasons.append("QUOTE_TIME_UNAVAILABLE_OR_FUTURE")
    # TrustedClock guarantees <=2s current UTC uncertainty. Include that bound in
    # addition to the source clock uncertainty rather than treating UTC as exact.
    age = Decimal(str(max(ages))) + uncertainty + Decimal("2") if ages else Decimal("Infinity")
    if age > policy.maximum_quote_age_seconds:
        reasons.append("QUOTE_STALE")
    if any(evidence.get(k) != "HEALTHY" for k in ("buy_quality", "sell_quality")):
        reasons.append("MARKET_CONFIDENCE_BELOW_HEALTHY")
    capital = amount(result.get("acquisition_cost_cad"))
    profit = amount(result.get("net_profit_cad"))
    pct = amount(result.get("net_return_pct"))
    for value, minimum, code in (
        (capital, policy.minimum_buy_capital_cad, "BUY_CAPITAL_BELOW_MINIMUM"),
        (capital, policy.minimum_liquidity_cad, "EXECUTABLE_LIQUIDITY_BELOW_MINIMUM"),
        (profit, policy.minimum_profit_cad, "PROFIT_BELOW_MINIMUM"),
        (pct, policy.minimum_return_pct, "RETURN_BELOW_MINIMUM"),
    ):
        if value is None or value < minimum:
            reasons.append(code)
    costs = result.get("costs", {})
    sell_required = amount(costs.get("sell_base_required"))
    quantity = amount(result.get("quantity"))
    if (
        sell_required is None
        or sell_required <= 0
        or quantity is None
        or quantity <= 0
        or not costs.get("capital_requirements")
    ):
        reasons.append("REQUIRED_INVENTORY_UNKNOWN")
    if capital is None or capital <= 0 or profit is None or profit <= 0:
        reasons.append("NON_POSITIVE_EXECUTABLE_CAPITAL_OR_PROFIT")
    if persistence_seconds < policy.minimum_persistence_seconds:
        reasons.append("PERSISTENCE_INSUFFICIENT")
    if actionable:
        # A result boolean cannot substitute for explicit, immutable confirmations.
        confirmations = result.get("actionability_confirmations", {})
        required = ("eligibility", "funding", "fees", "fx", "market_limits", "canonical_identity")
        for key in required:
            if confirmations.get(key) is not True:
                reasons.append(f"ACTIONABILITY_{key.upper()}_UNVERIFIED")
        if not evidence.get("clock", {}).get("trusted") or not evidence.get("clock", {}).get(
            "host_healthy"
        ):
            reasons.append("EVALUATION_CLOCK_NOT_HEALTHY")
    elif not policy.research_enabled:
        reasons.append("RESEARCH_NOTIFICATIONS_DISABLED")
    quiet = quiet_hours(now, policy)
    emergency = bool(actionable and policy.emergency_alerts)
    emergency_reasons = []
    if emergency:
        for value, minimum, code in (
            (capital, policy.nighttime_minimum_buy_capital_cad, "EMERGENCY_BUY_CAPITAL"),
            (capital, policy.nighttime_minimum_liquidity_cad, "EMERGENCY_LIQUIDITY"),
            (profit, policy.nighttime_minimum_profit_cad, "EMERGENCY_PROFIT"),
            (pct, policy.nighttime_minimum_return_pct, "EMERGENCY_RETURN"),
        ):
            if value is None or value < minimum:
                emergency_reasons.append(code)
        if age > policy.nighttime_maximum_quote_age_seconds:
            emergency_reasons.append("EMERGENCY_QUOTE_AGE")
        if persistence_seconds < policy.nighttime_minimum_persistence_seconds:
            emergency_reasons.append("EMERGENCY_PERSISTENCE")
        emergency = not emergency_reasons and not reasons
    if quiet and not emergency and policy.quiet_delivery == "SUPPRESS":
        reasons.append("QUIET_HOURS_SUPPRESSED")
        if actionable and policy.emergency_alerts:
            reasons.extend(emergency_reasons)
    if not policy.telegram_enabled:
        reasons.append("TELEGRAM_DISABLED")
    if actionable and not policy.normal_alerts and not emergency:
        reasons.append("ORDINARY_ALERTS_DISABLED")
    return Gate(not reasons, classification, emergency, quiet and not emergency, tuple(reasons))
