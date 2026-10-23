"""Bounded plain-text messages from exact observed values, never AI-generated claims."""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from arbiq.signals.policy import amount, timestamp


def value(v):
    number = amount(v)
    return format(number, "f") if number is not None else "unavailable"


def display_value(v, *, places=8, minimum_places=0, currency=""):
    """Compact display only. Rounding never feeds a rule, journal or evidence field.

    Tiny nonzero values use six significant digits in scientific notation rather
    than appearing as zero. An approximation marker identifies any lost precision.
    """
    number = amount(v)
    if number is None:
        return "unavailable"
    quantum = Decimal(1).scaleb(-places)
    if number != 0 and (abs(number) < quantum or number.adjusted() >= 15):
        mantissa, exponent = format(number, ".5E").split("E")
        rendered = f"{mantissa.rstrip('0').rstrip('.')}e{int(exponent)}"
        rounded = Decimal(rendered)
    else:
        with localcontext() as context:
            context.prec = max(32, len(number.as_tuple().digits) + places + 2)
            rounded = number.quantize(quantum, rounding=ROUND_HALF_EVEN)
        rendered = format(rounded, "f")
        if "." in rendered:
            whole, fractional = rendered.split(".", 1)
            fractional = fractional.rstrip("0").ljust(minimum_places, "0")
            rendered = whole + ("." + fractional if fractional else "")
    marker = "≈" if rounded != number else ""
    return marker + currency + rendered


def cad(v):
    return display_value(v, places=2, minimum_places=2, currency="C$")


def alert_message(result, now, app_url, *, emergency=False):
    costs = result.get("costs", {})
    label = "ACTIONABLE ESTIMATE" if result.get("actionable") is True else "RESEARCH ONLY"
    source = timestamp(result.get("supporting_source_at"))
    age = (
        f"{(now - source).total_seconds():.1f}s"
        if source and source <= now
        else "unknown / future timestamp"
        if source
        else "unknown"
    )
    buy_symbol = result.get("buy_symbol", "unknown quote")
    sell_symbol = result.get("sell_symbol", "unknown quote")
    text = (
        f"QUANTIX — {'URGENT ' if emergency else ''}{label}\n"
        f"{result.get('base_asset', 'Unknown asset')}\n"
        f"BUY: {result.get('buy_exchange', 'unknown')} — {display_value(result.get('buy_price'))} "
        f"({buy_symbol})\n"
        f"SELL: {result.get('sell_exchange', 'unknown')} — "
        f"{display_value(result.get('sell_price'))} ({sell_symbol})\n"
        f"Buy Capital: {cad(result.get('acquisition_cost_cad'))}\n"
        f"Sell inventory: {display_value(costs.get('sell_base_required'))} "
        f"{result.get('base_asset', '')}\n"
        f"Estimated Net: {cad(result.get('net_profit_cad'))} "
        f"({display_value(result.get('net_return_pct'), places=4, minimum_places=2)}%)\n"
        f"Quote age: {age} • {now.isoformat()}\n"
        "Observed estimate; historical prices may no longer be executable. PAPER ONLY."
    )
    if label == "RESEARCH ONLY":
        text += "\nMissing confirmations: " + ", ".join(result.get("reasons", [])[:5])
    detail_url = f"{app_url.rstrip('/')}/opportunities?opportunity={result.get('id', '')}"
    return text + f"\nExact values / recheck: {detail_url}"
