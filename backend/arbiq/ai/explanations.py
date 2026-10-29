"""Useful server-authored explanations, based only on deterministic public metrics."""

from decimal import Decimal

REASON_TEXT = {
    "HOST_CLOCK_UNHEALTHY": (
        "The host clock is unhealthy; corroborated UTC and uncertainty are required."
    ),
    "UTC_CLOCK_UNTRUSTED": "UTC is untrusted, so execution and verified alerts fail closed.",
    "UTC_UNTRUSTED": "UTC is untrusted, so execution and verified alerts fail closed.",
    "CANADIAN_PROVINCIAL_ACCOUNT_ELIGIBILITY_UNVERIFIED": (
        "Alberta account eligibility is unverified; public feed access does not approve trading."
    ),
    "PRE_FUNDED_INVENTORY_ASSUMED_NOT_VERIFIED": (
        "Both buying currency and selling inventory must be prefunded on their separate venues."
    ),
    "BUY_FEE_UNVERIFIED": (
        "The buy fee is an unverified assumption, rather than an account-"
        "specific confirmed schedule."
    ),
    "SELL_FEE_UNVERIFIED": (
        "The sell fee is an unverified assumption, rather than an account-"
        "specific confirmed schedule."
    ),
    "BUY_MARKET_LIMITS_UNVERIFIED": "Buy-side exchange precision or limits are unverified.",
    "SELL_MARKET_LIMITS_UNVERIFIED": "Sell-side exchange precision or limits are unverified.",
    "BUY_CAD_CONVERSION_UNAVAILABLE": "Buying currency lacks executable CAD acquisition evidence.",
    "SELL_CAD_CONVERSION_UNAVAILABLE": "Selling proceeds lack executable CAD liquidation evidence.",
    "BUY_CAD_CONVERSION_STALE": "Buy-side FX evidence was stale at the decision.",
    "SELL_CAD_CONVERSION_STALE": "Sell-side FX evidence was stale at the decision.",
    "BUY_CAD_CONVERSION_UNVERIFIED": "Buy-side FX assumptions are unverified.",
    "SELL_CAD_CONVERSION_UNVERIFIED": "Sell-side FX assumptions are unverified.",
    "MINIMUM_BUY_CAPITAL": (
        "Actual fee-inclusive buy capital is below the independent minimum buy rule."
    ),
    "MINIMUM_LIQUIDITY": "The candidate fails the separate paper liquidity threshold.",
    "INSUFFICIENT_FUNDING": "The virtual portfolio cannot fund both required exchange legs.",
    "PROFIT_THRESHOLD": "The deterministic candidate fails the configured profit thresholds.",
    "NON_POSITIVE_NET_AFTER_COSTS": (
        "Estimated net cashflow after included fees and reserves is not positive."
    ),
    "OPTIMIZER_GLOBAL_OPTIMUM_UNCONFIRMED": (
        "The bounded search found a feasible size but did not certify the global optimum."
    ),
    "REBALANCING_AND_SLIPPAGE_ASSUMPTIONS_UNVERIFIED": (
        "Slippage and rebalancing reserves are assumptions; they do not promise future fills."
    ),
    "NO_RETAINED_VALUATION": (
        "No retained portfolio valuation is available; unknown equity is not zero."
    ),
}


def display_decimal(value: str) -> str:
    """Human text only; preserve exact report/evidence strings in the response.

    Formatting never changes a financial decision or rounds significant digits.
    Extremely small exponents retain their exact scientific notation rather than
    expanding an arbitrarily long string.
    """
    number = Decimal(value)
    if number.is_zero():
        return "0"
    if number.as_tuple().exponent < -120:
        return str(number)
    fixed = format(number, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed


def deterministic_notes(report):
    values = report.numeric_inputs
    notes = {
        "paper_only": (
            "These are observed-data projections or virtual results. No real "
            "exchange order or realized real-world profit is represented."
        )
    }
    if report.source_type == "opportunity":
        notes["historical"] = (
            "This explanation uses retained evidence at its original evaluation "
            "time. Recheck before interpreting current market conditions."
        )
        capital = values.get("buy_capital_cad")
        if capital is not None:
            budget = values.get("selected_budget_cad")
            budget_label = f"C${display_decimal(budget)}" if budget is not None else "unavailable"
            notes["capital"] = (
                f"Executable projected buy capital is C${display_decimal(capital)}; "
                "the selected maximum "
                f"budget is {budget_label}. "
                "Sell-side inventory is separate."
            )
        profit = values.get("net_profit_cad")
        if profit is not None:
            pct = values.get("net_return_pct")
            pct_label = f"{display_decimal(pct)}%" if pct is not None else "unavailable"
            notes["profit"] = (
                f"Estimated net CAD profit is C${display_decimal(profit)}; estimated return is "
                f"{pct_label}. This is not executed profit."
            )
        else:
            notes["profit"] = (
                "CAD profit is unavailable because required depth or conversion "
                "evidence is incomplete."
            )
        if capital is not None and Decimal(capital) < Decimal(50):
            notes["small_size"] = (
                "This size is below the default C$50 minimum buy capital for "
                "qualifying alerts; a high percentage alone does not establish "
                "meaningful capacity."
            )
    else:
        notes["completed"] = (
            f"Completed simulations: {display_decimal(values.get('completed') or '0')}. "
            "Partial, failed and missed attempts are separate outcomes."
        )
        for key, label in (
            ("equity_cad", "Retained portfolio equity"),
            ("realized_simulated_pnl_cad", "Realized simulated disposal/FX P&L"),
            ("unrealized_pnl_cad", "Unrealized inventory P&L"),
            ("no_trading_benchmark_cad", "Original-holdings benchmark"),
        ):
            value = values.get(key)
            notes[key] = f"{label}: " + (
                f"C${display_decimal(value)}."
                if value is not None
                else "unavailable; no value is invented."
            )
        if values.get("completed") in {None, "0"}:
            notes["no_sample"] = (
                "Zero completed simulations cannot establish a profitable strategy or a win rate."
            )
    for code in report.reasons:
        notes[code] = REASON_TEXT[code]
    return notes


