"""Explicitly scoped public spot-market rules retained alongside CCXT metadata."""

from datetime import datetime
from decimal import Decimal

from arbiq.finance.contracts import exact_decimal, utc_timestamp

VERSION = "public-spot-rules-v1"
PUBLIC_RULE_SOURCES = {
    "coinbase": (
        "https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/"
        "public/list-public-products"
    ),
    "coinbaseexchange": (
        "https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/"
        "get-all-known-trading-pairs"
    ),
    "kraken": "https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs",
    "okx": "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-instruments",
}


def decimal_or_none(value) -> Decimal | None:
    if value is None or isinstance(value, float):
        return None
    try:
        return exact_decimal(value)
    except (ValueError, TypeError):
        return None


def positive_decimal(value) -> Decimal | None:
    number = decimal_or_none(value)
    return number if number is not None and number > 0 else None


def decimal_places_step(value) -> Decimal | None:
    number = decimal_or_none(value)
    if number is not None and number == number.to_integral_value() and 0 <= number <= 18:
        return Decimal(10) ** -int(number)
    return None


def _status(info: dict, key: str) -> str | None:
    value = info.get(key)
    return value if isinstance(value, str) and 0 < len(value) <= 32 else None


def _availability(state: str | None, live: str, non_market: set[str]) -> bool | None:
    if state == live:
        return True
    return False if state in non_market else None


def native_spot_rules(
    exchange: str, market: dict, observed_at: datetime | None
) -> tuple[dict[str, Decimal | None], dict]:
    """Return exact native overrides and bounded JSON provenance for a spot market.

    Missing or unsupported public parameters retain the normalized CCXT values
    for research where available. Each scoped limitation is carried into the
    immutable evaluation rather than inferred from a market-data socket state.
    """
    info = market.get("info")
    info = info if isinstance(info, dict) else {}
    limits: dict[str, Decimal | None] = {}
    source = PUBLIC_RULE_SOURCES.get(exchange, "CCXT public spot metadata")
    evidence = {"version": VERSION, "source": source, "market_order_available": None}
    if observed_at is not None:
        evidence["observed_at"] = utc_timestamp(observed_at).isoformat()
    if exchange == "kraken":
        status = _status(info, "status")
        evidence["market_order_available"] = _availability(
            status,
            "online",
            {
                "cancel_only",
                "delisted",
                "limit_only",
                "maintenance",
                "post_only",
                "reduce_only",
                "work_in_progress",
            },
        )
        limits.update(
            amount_step=decimal_places_step(info.get("lot_decimals")),
            price_tick=positive_decimal(info.get("tick_size")),
            minimum_quantity=positive_decimal(info.get("ordermin")),
            minimum_notional=positive_decimal(info.get("costmin")),
        )
        if (cost_step := decimal_places_step(info.get("cost_decimals"))) is not None:
            evidence["cost_step"] = str(cost_step)
    elif exchange == "coinbaseexchange" or (
        exchange == "coinbase" and info.get("product_type") == "SPOT"
    ):
        status = _status(info, "status")
        flag_names = ("cancel_only", "post_only", "limit_only", "auction_mode")
        if exchange == "coinbase":
            flag_names += ("is_disabled", "view_only", "trading_disabled")
        flags = {name: info[name] for name in flag_names if isinstance(info.get(name), bool)}
        if isinstance(info.get("trading_disabled"), bool):
            flags["trading_disabled"] = info["trading_disabled"]
        if any(flags.values()) or status in {"offline", "internal", "delisted"}:
            evidence["market_order_available"] = False
        elif status == "online" and all(name in flags for name in flag_names):
            evidence["market_order_available"] = True
        limits.update(
            amount_step=positive_decimal(info.get("base_increment")),
            price_tick=positive_decimal(
                info.get("price_increment" if exchange == "coinbase" else "quote_increment")
            ),
        )
        if exchange == "coinbase":
            limits.update(
                minimum_quantity=positive_decimal(info.get("base_min_size")),
                maximum_quantity=positive_decimal(info.get("base_max_size")),
                minimum_notional=positive_decimal(info.get("quote_min_size")),
                maximum_notional=positive_decimal(info.get("quote_max_size")),
            )
            if (cost_step := positive_decimal(info.get("quote_increment"))) is not None:
                evidence["cost_step"] = str(cost_step)
        else:
            evidence["limitation"] = "COINBASE_EXCHANGE_MARKET_MINIMUM_NOT_PUBLICLY_CONFIRMED"
        if flags:
            evidence["status_flags"] = sorted(
                name for name, restricted in flags.items() if restricted
            )
    elif exchange == "okx" and info.get("instType") == "SPOT":
        status = _status(info, "state")
        evidence["market_order_available"] = _availability(
            status, "live", {"suspend", "rebase", "post_only", "preopen", "test"}
        )
        limits.update(
            amount_step=positive_decimal(info.get("lotSz")),
            price_tick=positive_decimal(info.get("tickSz")),
            minimum_quantity=positive_decimal(info.get("minSz")),
        )
        evidence["limitation"] = "OKX_MARKET_CAPS_HAVE_EXTERNAL_DENOMINATIONS"
        for field, unit in (("maxMktSz", "USDT"), ("maxMktAmt", "USD")):
            if (cap := positive_decimal(info.get(field))) is not None:
                evidence[f"{field}_{unit}"] = str(cap)
    else:
        status = None
    if status is not None:
        evidence["native_status"] = status
    elif market.get("active") is False:
        evidence["market_order_available"] = False
    return ({key: value for key, value in limits.items() if value is not None}, evidence)
