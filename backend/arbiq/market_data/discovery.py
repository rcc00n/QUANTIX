from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from arbiq.collector.config import CollectorConfig
from arbiq.market_data.identity import AssetIdentity, asset_identity
from arbiq.market_data.trading_rules import (
    decimal_or_none,
    decimal_places_step,
    native_spot_rules,
    positive_decimal,
)


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    native_symbol: str
    native_id: str
    base: AssetIdentity
    quote: AssetIdentity
    active: bool | None
    amount_step: Decimal | None
    price_tick: Decimal | None
    min_notional: Decimal | None
    quote_volume: Decimal | None
    metadata: dict
    id: UUID | None = None


def normalize_market(
    exchange: str,
    market: dict,
    currencies: dict,
    ticker: dict | None = None,
    precision_mode: int | None = None,
    *,
    identity_by_symbol: dict[str, AssetIdentity] | None = None,
    rules_observed_at: datetime | None = None,
) -> DiscoveredMarket:
    if market.get("spot") is not True or market.get("contract"):
        raise ValueError("Only explicit spot markets are supported")
    if not all(
        isinstance(market.get(key), str) and market[key]
        for key in ("symbol", "base", "quote", "id")
    ):
        raise ValueError("Incomplete market identity")
    if any(
        len(market[key]) > limit for key, limit in (("symbol", 100), ("base", 32), ("quote", 32))
    ):
        raise ValueError("Market identifiers exceed supported bounds")

    def identity(symbol):
        # Only share immutable identities within one discovery snapshot of one
        # exchange. A later snapshot or another venue may resolve it differently.
        if identity_by_symbol is None:
            return asset_identity(exchange, symbol, currencies.get(symbol))
        result = identity_by_symbol.get(symbol)
        if result is None:
            result = asset_identity(exchange, symbol, currencies.get(symbol))
            identity_by_symbol[symbol] = result
        return result

    base = identity(market["base"])
    quote = identity(market["quote"])
    if base.key == quote.key:
        raise ValueError("Market base and quote identities are identical")
    precision = market.get("precision") or {}
    limits = market.get("limits") or {}
    amount_limits = limits.get("amount") or {}
    cost_limits = limits.get("cost") or {}
    native_limits, trading_rules = native_spot_rules(exchange, market, rules_observed_at)
    min_notional = native_limits.get("minimum_notional") or decimal_or_none(cost_limits.get("min"))
    min_quantity = native_limits.get("minimum_quantity") or decimal_or_none(
        amount_limits.get("min")
    )
    max_quantity = native_limits.get("maximum_quantity") or decimal_or_none(
        amount_limits.get("max")
    )
    max_notional = native_limits.get("maximum_notional") or decimal_or_none(cost_limits.get("max"))

    # CCXT TICK_SIZE=4; DECIMAL_PLACES=2. Significant-digit precision is not a tick.
    def step(value):
        if precision_mode == 4:
            return positive_decimal(value)
        if precision_mode == 2:
            return decimal_places_step(value)
        return None

    active = market.get("active")
    if not isinstance(active, bool):
        active = None
    ticker = ticker or {}
    volume = decimal_or_none(ticker.get("quoteVolume"))
    return DiscoveredMarket(
        native_symbol=market["symbol"],
        native_id=market["id"],
        base=base,
        quote=quote,
        active=active,
        amount_step=native_limits.get("amount_step") or step(precision.get("amount")),
        price_tick=native_limits.get("price_tick") or step(precision.get("price")),
        min_notional=min_notional,
        quote_volume=volume,
        metadata={
            "base_symbol": base.symbol,
            "quote_symbol": quote.symbol,
            "native_id": market["id"],
            "base_identity_verified": base.verified,
            "quote_identity_verified": quote.verified,
            "base_identity_reason": base.reason,
            "quote_identity_reason": quote.reason,
            "active_status": "active"
            if active is True
            else "inactive"
            if active is False
            else "unknown",
            "spot": True,
            "quote_volume": str(volume) if volume is not None else None,
            "quote_volume_unit": quote.symbol,
            "precision_mode": precision_mode,
            "trading_rules": trading_rules,
            "minimum_quantity": str(min_quantity) if min_quantity is not None else None,
            "maximum_quantity": str(max_quantity) if max_quantity is not None else None,
            "maximum_notional": str(max_notional) if max_notional is not None else None,
            **(
                {"fx_stablecoin": market["info"]["fx_stablecoin"]}
                if exchange == "coinbaseexchange"
                and isinstance(market.get("info"), dict)
                and isinstance(market["info"].get("fx_stablecoin"), bool)
                else {}
            ),
            "advertised_taker_rate": str(v)
            if (v := decimal_or_none(market.get("taker"))) is not None
            else None,
            "fee_source": "CCXT public market metadata; account tier/currency unverified",
        },
    )


def eligible_markets(
    markets: list[DiscoveredMarket], config: CollectorConfig
) -> list[DiscoveredMarket]:
    eligible = []
    for market in markets:
        if (
            config.base_assets
            and market.base.symbol not in config.base_assets
            and market.base.key not in config.base_assets
        ):
            continue
        if config.quote_assets and market.quote.symbol not in config.quote_assets:
            continue
        if config.active_only and market.active is not True:
            if market.active is False or not config.allow_unknown_active:
                continue
        if config.minimum_quote_volume > 0 and (
            market.quote_volume is None or market.quote_volume < config.minimum_quote_volume
        ):
            continue
        eligible.append(market)
    return eligible


def selected_markets(
    markets: list[DiscoveredMarket], config: CollectorConfig
) -> list[DiscoveredMarket]:
    eligible = eligible_markets(markets, config)

    def rank(market):
        return (
            0 if config.prefer_cad and market.quote.symbol == "CAD" else 1,
            config.priority_assets.index(market.base.symbol)
            if market.base.symbol in config.priority_assets
            else len(config.priority_assets),
            config.quote_assets.index(market.quote.symbol)
            if market.quote.symbol in config.quote_assets
            else len(config.quote_assets),
            -(market.quote_volume or Decimal(0)),
            market.native_symbol,
        )

    ordered = sorted(eligible, key=rank)
    # Spread a small VPS budget across base assets before adding redundant quotes.
    distinct, remainder, bases = [], [], set()
    for market in ordered:
        if market.base.key in bases:
            remainder.append(market)
        else:
            distinct.append(market)
            bases.add(market.base.key)
    return (distinct + remainder)[: config.max_markets_per_exchange]
