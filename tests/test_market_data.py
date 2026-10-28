"""All markets, prices and venues in these tests are explicitly SYNTHETIC."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import ccxt
import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import ClockGuard, Schedule, retry_delay
from arbiq.connectors.ccxt_public import PublicConnector, classify_error
from arbiq.connectors.registry import available_candidates, capabilities, eligibility
from arbiq.market_data.cache import BookCache
from arbiq.market_data.discovery import normalize_market, selected_markets
from arbiq.market_data.identity import asset_identity
from arbiq.market_data.quality import DataState, fresh_view, validate_book
from arbiq.notifications.urgent import UrgencyPolicy
from pydantic import ValidationError


def synthetic_market(base="BTC", quote="USD", **changes):
    return {
        "id": f"{base}-{quote}",
        "symbol": f"{base}/{quote}",
        "base": base,
        "quote": quote,
        "spot": True,
        "contract": False,
        "active": True,
        "precision": {"amount": "0.0001", "price": "0.01"},
        "limits": {"cost": {"min": "1"}},
        **changes,
    }


def synthetic_raw(now=None):
    now = now or datetime.now(UTC)
    return {
        "symbol": "BTC/USD",
        "timestamp": int(now.timestamp() * 1000),
        "bids": [[str(100 - i), "2"] for i in range(10)],
        "asks": [[str(101 + i), "3"] for i in range(10)],
        "nonce": 123,
    }


def synthetic_capture(raw=None, **kwargs):
    now = kwargs.pop("received_at", datetime.now(UTC))
    return validate_book(
        synthetic_raw(now) if raw is None else raw,
        market_id=kwargs.pop("market_id", uuid4()),
        exchange=kwargs.pop("exchange", "synthetic-a"),
        symbol=kwargs.pop("symbol", "BTC/USD"),
        base_key=kwargs.pop("base_key", "asset:BTC"),
        quote_key=kwargs.pop("quote_key", "fiat:USD"),
        received_at=now,
        request_started_at=now - timedelta(milliseconds=20),
        latency_ms=20,
        config=kwargs.pop("config", CollectorConfig()),
        **kwargs,
    )


def test_identity_does_not_merge_wrappers_unknowns_or_stablecoins():
    assert asset_identity("synthetic-a", "BTC").key == asset_identity("synthetic-b", "BTC").key
    assert asset_identity("synthetic-a", "WBTC").key != asset_identity("synthetic-a", "BTC").key
    assert not asset_identity("synthetic-a", "BTC", {"name": "Wrapped Bitcoin"}).verified
    assert not asset_identity("synthetic-a", "SOL", {"name": "Solana Wormhole"}).verified
    assert (
        asset_identity("synthetic-a", "CUSTOM").key != asset_identity("synthetic-b", "CUSTOM").key
    )
    assert (
        len({asset_identity("synthetic-a", code).key for code in ("CAD", "USD", "USDC", "USDT")})
        == 4
    )
    assert asset_identity("synthetic-a", "USDC").kind == "stablecoin"


def test_discovery_shares_identities_only_within_its_snapshot():
    identity_by_symbol = {}
    normal = normalize_market(
        "synthetic", synthetic_market(), {}, identity_by_symbol=identity_by_symbol
    )
    same_base = normalize_market(
        "synthetic", synthetic_market(quote="CAD"), {}, identity_by_symbol=identity_by_symbol
    )
    rediscovered = normalize_market(
        "synthetic", synthetic_market(), {"BTC": {"name": "Wrapped Bitcoin"}}, identity_by_symbol={}
    )
    assert normal.base is same_base.base
    assert normal.base.verified and not rediscovered.base.verified
    assert normal.base.key != rediscovered.base.key


def test_dynamic_discovery_precision_filtering_and_base_diversity():
    markets = [
        normalize_market(
            "synthetic", synthetic_market(base, quote), {}, {"quoteVolume": "200000"}, 4
        )
        for base in ("BTC", "ETH", "SOL", "OTHER")
        for quote in ("CAD", "USD")
    ]
    assert all(m.price_tick == Decimal("0.01") for m in markets)
    config = CollectorConfig(max_markets_per_exchange=4)
    selected = selected_markets(markets, config)
    assert len({m.base.key for m in selected}) == 4
    assert all(m.quote.symbol == "CAD" for m in selected)
    assert any(m.base.symbol == "OTHER" for m in selected)  # Not a fixed seven-asset universe.
    filtered = selected_markets(markets, CollectorConfig(base_assets=["ETH"], quote_assets=["USD"]))
    assert [m.native_symbol for m in filtered] == ["ETH/USD"]
    assert not selected_markets(markets, CollectorConfig(minimum_quote_volume="999999"))
    assert not selected_markets(
        [replace(markets[0], active=None)], CollectorConfig(allow_unknown_active=False)
    )
    assert selected_markets(
        [replace(markets[0], active=None)], CollectorConfig(allow_unknown_active=True)
    )
    with pytest.raises(ValueError):
        normalize_market("synthetic", synthetic_market(spot=False), {})
    with pytest.raises(ValueError):
        normalize_market("synthetic", synthetic_market(contract=True), {})
    with pytest.raises(ValueError):
        normalize_market("synthetic", synthetic_market(base=None), {})
    places = normalize_market(
        "synthetic",
        synthetic_market(precision={"amount": 8, "price": 2}),
        {},
        precision_mode=2,
    )
    assert places.amount_step == Decimal("0.00000001")


def test_capability_and_eligibility_detection_never_infers_manual_eligibility():
    supported, missing = available_candidates(["kraken", "synthetic-absent", "ndax"], ["ndax"])
    assert supported == ["kraken"] and missing == ["synthetic-absent"]
    assert eligibility("kraken")["classification"] == "REGISTERED_ENTITY"
    assert eligibility("synthetic-venue")["classification"] == "UNVERIFIED"
    assert not eligibility("kraken")["manual_trading_eligible"]
    adapter = type(
        "SyntheticAdapter",
        (),
        {"id": "synthetic-venue", "has": {"spot": True, "fetchOrderBook": True}},
    )()
    caps = capabilities(adapter, [synthetic_market(quote="CAD")])
    assert caps["cad_market_available"] and caps["order_book_rest"]
    assert not caps["public_market_data_verified"]


def test_healthy_book_is_exact_and_never_an_opportunity():
    book = synthetic_capture()
    assert book.state == DataState.HEALTHY
    assert book.normalized.bids[0].price == Decimal(100)
    assert book.actionable is False
    assert isinstance(book.model_dump(mode="json")["normalized"]["bids"][0]["price"], str)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("crossed", DataState.SUSPICIOUS),
        ("unordered", DataState.SUSPICIOUS),
        ("negative", DataState.SUSPICIOUS),
        ("float", DataState.SUSPICIOUS),
        ("empty", DataState.UNAVAILABLE),
        ("malformed", DataState.UNAVAILABLE),
        ("shallow", DataState.DEGRADED),
        ("timestamp_missing", DataState.DEGRADED),
        ("timestamp_future", DataState.SUSPICIOUS),
        ("timestamp_naive", DataState.SUSPICIOUS),
        ("stale", DataState.STALE),
        ("symbol_mismatch", DataState.SUSPICIOUS),
        ("nan", DataState.SUSPICIOUS),
    ],
)
def test_quality_flags_and_preserves_evidence(case, expected):
    now = datetime.now(UTC)
    raw = synthetic_raw(now)
    if case == "crossed":
        raw["asks"][0][0] = "99"
    if case == "unordered":
        raw["bids"][0], raw["bids"][1] = raw["bids"][1], raw["bids"][0]
    if case == "negative":
        raw["bids"][0][1] = "-1"
    if case == "float":
        raw["bids"][0][0] = 100.1
    if case == "empty":
        raw["asks"] = []
    if case == "malformed":
        raw = "synthetic-invalid-payload"
    if case == "shallow":
        raw["asks"] = raw["asks"][:1]
    if case == "timestamp_missing":
        raw["timestamp"] = None
    if case == "timestamp_future":
        raw["timestamp"] = int((now + timedelta(seconds=20)).timestamp() * 1000)
    if case == "timestamp_naive":
        raw["timestamp"] = "2026-11-21T12:00:00"
    if case == "stale":
        raw["timestamp"] = int((now - timedelta(minutes=10)).timestamp() * 1000)
    if case == "symbol_mismatch":
        raw["symbol"] = "ETH/USD"
    if case == "nan":
        raw["bids"][0][1] = "NaN"
    book = synthetic_capture(raw, received_at=now)
    assert book.state == expected
    assert book.reasons and book.raw_evidence
    assert book.actionable is False


def test_ingestion_freshness_identity_clock_and_liquidity_gates():
    now = datetime.now(UTC)
    book = synthetic_capture(received_at=now)
    assert fresh_view(book, now + timedelta(seconds=61), 60).state == DataState.STALE
    assert fresh_view(book, now + timedelta(seconds=61), 60).normalized.quality == "stale"
    assert fresh_view(book, now - timedelta(seconds=10), 60).state == DataState.SUSPICIOUS
    assert "unverified_asset_identity" in synthetic_capture(identity_verified=False).reasons
    assert "collector_clock_jump" in synthetic_capture(clock_healthy=False).reasons
    assert (
        "insufficient_quote_liquidity"
        in synthetic_capture(config=CollectorConfig(minimum_book_notional="999999")).reasons
    )


def test_reference_confirmation_keeps_quote_units_and_independent_venues():
    cache = BookCache(CollectorConfig())
    for exchange in ("synthetic-a", "synthetic-b"):
        cache.put(synthetic_capture(exchange=exchange))
    raw = synthetic_raw()
    raw["bids"] = [[str(200 - i), "1"] for i in range(10)]
    raw["asks"] = [[str(201 + i), "1"] for i in range(10)]
    candidate = synthetic_capture(raw, exchange="synthetic-c")
    suspect = cache.put(candidate)
    assert suspect.state == DataState.SUSPICIOUS
    assert suspect.normalized.quality != "valid"
    assert "extreme_reference_discrepancy" in suspect.reasons
    repeated = cache.put(candidate, confirmation=True)
    assert repeated.verification["status"] == "REPEATED_WITH_REFERENCE_DISAGREEMENT"
    assert repeated.state == DataState.SUSPICIOUS
    recovered = cache.put(
        synthetic_capture(exchange="synthetic-c", market_id=candidate.market_id),
        confirmation=True,
    )
    assert recovered.state == DataState.HEALTHY
    assert recovered.verification["status"] == "CONFIRMED"
    assert not cache.references(synthetic_capture(quote_key="asset:USDT"), datetime.now(UTC))
    pair = BookCache(CollectorConfig())
    pair.put(synthetic_capture(exchange="coinbaseexchange"))
    assert not pair.references(synthetic_capture(exchange="coinbase"), datetime.now(UTC))


def test_cache_is_bounded_and_delayed_records_do_not_replace_newer_books():
    cache = BookCache(CollectorConfig(exchange_ids=["synthetic"], max_markets_per_exchange=2))
    for _ in range(3):
        cache.put(synthetic_capture())
    assert len(cache.books) == 2
    book = synthetic_capture()
    cache.put(book)
    older = synthetic_capture(
        market_id=book.market_id, received_at=book.received_at - timedelta(seconds=1)
    )
    assert cache.put(older).received_at == book.received_at
    sequenced = book.model_copy(update={"ingestion_index": 1})
    cache.put(sequenced)
    clock_jump = older.model_copy(
        update={
            "ingestion_index": 2,
            "state": DataState.SUSPICIOUS,
            "reasons": ("collector_clock_jump",),
        }
    )
    # A later ingestion counter never authorizes replacing newer UTC evidence.
    assert cache.put(clock_jump).ingestion_index == 1
    assert cache.rejected_updates["OUT_OF_ORDER_RECEIPT"] == 2


@pytest.mark.parametrize(
    "error,code",
    [
        (ccxt.RequestTimeout("synthetic"), "TIMEOUT"),
        (TimeoutError(), "TIMEOUT"),
        (ccxt.RateLimitExceeded("synthetic"), "RATE_LIMIT"),
        (ccxt.AuthenticationError("synthetic"), "PUBLIC_ENDPOINT_REQUIRES_AUTH"),
        (ccxt.PermissionDenied("synthetic"), "ACCESS_DENIED"),
        (ccxt.BadSymbol("synthetic"), "MARKET_UNAVAILABLE"),
        (ccxt.BadRequest("synthetic"), "UNSUPPORTED_REQUEST"),
        (OSError("synthetic"), "NETWORK_UNAVAILABLE"),
        (ValueError("synthetic"), "MALFORMED_RESPONSE"),
    ],
)
def test_exchange_error_classification(error, code):
    assert classify_error(error) == code


def test_backoff_rate_limits_and_clock_recovery():
    config = CollectorConfig()
    delays = [retry_delay(n, config, jitter=0) for n in range(1, 15)]
    assert delays == sorted(delays)
    assert max(delays) <= config.retry_cap_seconds
    assert retry_delay(1, config, rate_limited=True, jitter=0) >= 30
    schedule = Schedule()
    schedule.failure(config, "RATE_LIMIT")
    assert schedule.failures == 1
    schedule.success(1)
    assert schedule.failures == 0
    clock = ClockGuard()
    wall, mono = clock.last_wall, clock.last_monotonic
    assert not clock.check(wall + timedelta(seconds=60), mono + 1)
    for index in range(1, 6):
        clock.check(wall + timedelta(seconds=60 + index), mono + 1 + index)
    assert clock.healthy


async def test_ccxt_l2_decimal_hook_and_discrete_depths_without_network():
    connector = PublicConnector("kraken", CollectorConfig())
    try:
        values = connector.adapter.parse_order_book_bid_ask(
            ["1.123456789123456789", "0.000000000000000001"]
        )
        assert values == [
            Decimal("1.123456789123456789"),
            Decimal("0.000000000000000001"),
        ]
        assert connector.adapter.safe_number(
            {"precision": Decimal("0.0001")}, "precision"
        ) == Decimal("0.0001")
        assert connector.adapter.enableRateLimit
    finally:
        await connector.close()
    connector = PublicConnector("kucoin", CollectorConfig())
    assert connector.requested_depth(25) == (100, {})
    await connector.close()
    connector = PublicConnector("binance", CollectorConfig())
    assert connector.adapter.options["fetchMarkets"]["types"] == ["spot"]
    await connector.close()
    connector = PublicConnector("htx", CollectorConfig())
    assert connector.requested_depth(25) == (150, {})
    await connector.close()
    connector = PublicConnector("bitfinex", CollectorConfig())
    assert connector.requested_depth(25) == (25, {"precision": "P0"})
    from unittest.mock import AsyncMock

    connector._call = AsyncMock(
        return_value={
            "timestamp": 123,
            "datetime": "synthetic-local",
            "bids": [],
            "asks": [],
        }
    )
    assert (await connector.fetch_raw_book("BTC/USD"))["timestamp"] is None
    await connector.close()


async def test_public_request_serialization_timeout_and_pool_cleanup():
    connector = PublicConnector("kraken", CollectorConfig(minimum_request_interval_seconds=0.1))
    active = 0
    maximum = 0

    async def fake_public_request():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"synthetic": True}

    try:
        values = await asyncio.gather(
            connector._call(fake_public_request), connector._call(fake_public_request)
        )
        assert maximum == 1 and len(values) == 2
        with pytest.raises(TimeoutError):
            await connector._call(lambda: asyncio.sleep(10), deadline_seconds=0.01)
    finally:
        await connector.close()
    assert connector._session is None


def test_urgent_alert_contracts_default_off_paid_disabled_and_nighttime_stricter():
    policy = UrgencyPolicy()
    assert not policy.enabled and not policy.voice_enabled
    assert policy.timezone == "America/Edmonton"
    with pytest.raises(ValidationError):
        UrgencyPolicy(nighttime_minimum_cad="1")
    assert UrgencyPolicy(enabled=True).enabled  # Stage6 Telegram-only opt-in is implemented.
    with pytest.raises(ValidationError):
        UrgencyPolicy(voice_enabled=True)
