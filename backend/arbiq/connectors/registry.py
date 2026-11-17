"""Capability discovery and sourced eligibility; public access never approves trading."""

from datetime import UTC, datetime

import ccxt
import ccxt.async_support as async_ccxt

from arbiq.connectors.canadian_eligibility import eligibility as eligibility


def available_candidates(requested: list[str], excluded: list[str]) -> tuple[list[str], list[str]]:
    selected = [name for name in requested if name not in excluded]
    return (
        [name for name in selected if name in async_ccxt.exchanges],
        [name for name in selected if name not in async_ccxt.exchanges],
    )


def capabilities(adapter, markets: list[dict] | None = None) -> dict:
    has = adapter.has
    websocket = adapter.id in {"kraken", "coinbaseexchange", "okx"}
    try:
        import ccxt.pro as pro

        if adapter.id in pro.exchanges:
            probe = getattr(pro, adapter.id)()
            websocket_advertised = bool(probe.has.get("watchOrderBook"))
        else:
            websocket_advertised = False
    except (ImportError, AttributeError):
        websocket_advertised = False
    spot = markets or []
    return {
        "ccxt_version": ccxt.__version__,
        "adapter": adapter.id,
        "public_market_data_supported": bool(has.get("fetchOrderBook")),
        "public_market_data_verified": False,
        "spot_supported": bool(has.get("spot")),
        "cad_market_available": any(m.get("quote") == "CAD" for m in spot),
        "order_book_rest": bool(has.get("fetchOrderBook")),
        "websocket_advertised": websocket_advertised,
        "websocket_implemented": websocket,
        "canadian_eligibility": eligibility(adapter.id),
        "discovered_at": datetime.now(UTC).isoformat(),
    }
