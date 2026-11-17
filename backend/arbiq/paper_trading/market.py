"""Observed liquidation marks; missing depth/FX remains unavailable, never parity."""

from datetime import timedelta
from decimal import Decimal, localcontext

from sqlalchemy import select

from arbiq.db.models import Asset, Market
from arbiq.market_data.quality import DataState
from arbiq.opportunities.analysis import dump_input
from arbiq.opportunities.conversions import (
    FX_ASSET_KEYS,
    capture_uncertainty_ms,
    synchronized_conversion,
)
from arbiq.opportunities.math import convert, fill_depth
from arbiq.portfolio.contracts import PriceEvidence


def conversion_rejection(conversion, now):
    if conversion is None:
        return "cad_conversion_unavailable"
    if conversion.currency == "CAD":
        return None  # Identity does not consume a timestamped conversion book.
    if conversion.observed_at is None or conversion.available_at is None:
        return "cad_conversion_timestamp_unavailable"
    if conversion.observed_at > now or conversion.available_at > now:
        return "cad_conversion_future"
    if conversion.fresh_until < now:
        return "cad_conversion_stale"
    return None


class PaperMarkets:
    def __init__(self, collector):
        self.collector = collector
        self.markets = {}
        self.assets = {}

    async def refresh(self, db):
        ids = list(self.collector.monitored_ids())
        rows = list(await db.scalars(select(Market).where(Market.id.in_(ids)))) if ids else []
        self.markets = {m.id: m for m in rows}
        asset_ids = {a for m in rows for a in (m.base_asset_id, m.quote_asset_id)}
        self.assets = {
            a.id: a
            for a in await db.scalars(
                select(Asset).where(Asset.id.in_(asset_ids) | (Asset.canonical_key == "fiat:CAD"))
            )
        }

    def mark(self, exchange_id, asset_id, quantity, now, research=True, *, fx=None):
        asset = self.assets.get(asset_id)
        if not asset:
            return PriceEvidence(None, None, None, source="asset_not_monitored")
        if asset.canonical_key == "fiat:CAD":
            return PriceEvidence(Decimal(1), now, now, True, "CAD identity")
        clock = self.collector.utc_clock
        if not clock or not clock.trusted:
            return PriceEvidence(None, None, None, source="utc_untrusted")
        if fx is None:
            fx = self.collector.scanner.fx_routes(now)
        limitations = []
        currency = FX_ASSET_KEYS.get(asset.canonical_key)
        if currency in fx:
            item = fx[currency]
            reason = conversion_rejection(item, now)
            try:
                if reason:
                    raise ValueError(reason)
                with localcontext() as ctx:
                    ctx.prec = 78
                    amount = convert(item.to_cad, quantity)
                    return PriceEvidence(
                        amount / quantity,
                        item.oldest_observed_at or item.observed_at,
                        item.available_at,
                        item.verified,
                        item.source,
                        {
                            "conversion": dump_input(item),
                            "clock": clock.snapshot(),
                            "valued_quantity": str(quantity),
                        },
                    )
            except (ValueError, ArithmeticError):
                limitations.append(reason or "cad_conversion_depth_unavailable")
        elif currency is not None:
            limitations.append("cad_conversion_unavailable")
        candidates = sorted(
            self.markets.values(), key=lambda m: (m.exchange_id != exchange_id, str(m.id))
        )
        for market in candidates:
            if market.base_asset_id != asset_id:
                continue
            book = self.collector.cache.get(market.id, now=now)
            if not book:
                limitations.append("valuation_book_unavailable")
                continue
            if book.received_at > now or (
                book.exchange_timestamp is not None and book.exchange_timestamp > now
            ):
                limitations.append("valuation_book_future")
                continue
            if (
                book.state == DataState.STALE
                or now - book.received_at > timedelta(seconds=30)
                or (
                    book.exchange_timestamp is not None
                    and now - book.exchange_timestamp > timedelta(seconds=30)
                )
            ):
                limitations.append("valuation_book_stale")
                continue
            if book.exchange_timestamp is None:
                limitations.append("valuation_source_timestamp_unavailable")
                continue
            if book.state not in (
                {DataState.HEALTHY, DataState.DEGRADED} if research else {DataState.HEALTHY}
            ):
                limitations.append("valuation_book_confidence_unavailable")
                continue
            quote = self.assets.get(market.quote_asset_id)
            if quote is None:
                limitations.append("valuation_quote_identity_unavailable")
                continue
            conversion = fx.get(FX_ASSET_KEYS.get(quote.canonical_key))
            reason = conversion_rejection(conversion, now)
            if reason:
                limitations.append(reason)
                continue
            if conversion.selection_version and not synchronized_conversion(
                conversion,
                book.exchange_timestamp,
                book.received_at,
                capture_uncertainty_ms(book.verification.get("clock") or {}),
                timedelta(seconds=self.collector.settings.arbitrage.maximum_book_skew_seconds),
            ):
                limitations.append("cad_conversion_capture_skew")
                continue
            try:
                with localcontext() as ctx:
                    ctx.prec = 78
                    notional = fill_depth(book.normalized.bids, quantity)
                    value = convert(conversion.to_cad, notional)
                    fx_observed_at = conversion.oldest_observed_at or conversion.observed_at
                    return PriceEvidence(
                        value / quantity,
                        min(book.exchange_timestamp, fx_observed_at)
                        if fx_observed_at is not None
                        else book.exchange_timestamp,
                        max(book.received_at, conversion.available_at)
                        if conversion.available_at is not None
                        else book.received_at,
                        book.state == DataState.HEALTHY
                        and conversion.verified
                        and market.exchange_id == exchange_id,
                        "Observed bid liquidation mark",
                        {
                            "market_id": str(market.id),
                            "content_sha256": book.content_sha256,
                            "valued_quantity": str(quantity),
                            "reference_venue": market.exchange_id != exchange_id,
                            "book": book.model_dump(mode="json"),
                            "fx_source": conversion.source,
                            "fx_observed_at": fx_observed_at.isoformat()
                            if fx_observed_at is not None
                            else None,
                            "fx_conversion": dump_input(conversion),
                            "fx_available_at": conversion.available_at.isoformat()
                            if conversion.available_at is not None
                            else None,
                            "clock": clock.snapshot(),
                        },
                    )
            except (ValueError, ArithmeticError):
                limitations.append("valuation_depth_unavailable")
        reasons = list(dict.fromkeys(limitations))
        # Different rejections can coexist across sources. Keep all reason codes
        # without attaching a numeric mark to any failed source.
        return PriceEvidence(
            None,
            None,
            None,
            source=reasons[0] if reasons else "valuation_market_unavailable",
            provenance={"valuation_blockers": reasons},
        )

    def prices(self, positions, now, research):
        positions = [p for p in positions if p.quantity > 0]
        clock = self.collector.utc_clock
        needs_fx = any(
            (asset := self.assets.get(p.asset_id)) and asset.canonical_key != "fiat:CAD"
            for p in positions
        )
        fx = self.collector.scanner.fx_routes(now) if needs_fx and clock and clock.trusted else {}
        return {
            (p.exchange_id, p.asset_id): self.mark(
                p.exchange_id, p.asset_id, p.quantity, now, research, fx=fx
            )
            for p in positions
        }
