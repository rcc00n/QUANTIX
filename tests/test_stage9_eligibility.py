"""Public eligibility evidence and synthetic exchange status snapshots."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from arbiq.collector.config import DEFAULT_EXCHANGES
from arbiq.connectors.canadian_eligibility import eligibility
from arbiq.market_data import api


def test_every_configured_adapter_keeps_account_and_product_verification_closed():
    reviews = {slug: eligibility(slug) for slug in DEFAULT_EXCHANGES}
    assert len(reviews) == 20
    for review in reviews.values():
        assert review["account_status"] == "UNVERIFIED"
        assert review["manual_trading_eligible"] is False
        assert review["product_verified"] is False
        assert review["owner_checklist"]
        assert all(item["url"].startswith("https://") for item in review["sources"])


@pytest.mark.parametrize("slug", ["kraken", "ndax", "coinbase"])
def test_documented_canadian_spot_has_province_specific_public_evidence(slug):
    review = eligibility(slug)
    assert review["public_status"] == "CANADIAN_SPOT_DOCUMENTED"
    assert review["alberta_status"] == "PUBLIC_SCOPE_DOCUMENTED"
    assert review["classification"] == "REGISTERED_ENTITY"
    assert review["reviewed_at"] == "2026-11-24"
    assert len(review["sources"]) > 1


def test_registered_retail_affiliates_never_verify_a_separate_exchange_product():
    retail, exchange, crypto_com = map(
        eligibility, ["coinbase", "coinbaseexchange", "cryptocom"]
    )
    assert exchange["venue_group"] == retail["venue_group"] == "coinbase"
    assert exchange["entity"] == retail["entity"]
    for review in (exchange, crypto_com):
        assert review["public_status"] == "SEPARATE_PRODUCT_UNVERIFIED"
        assert review["alberta_status"] == "UNVERIFIED"


@pytest.mark.parametrize("slug", ["okx", "bybit", "bitget", "mexc", "gate"])
def test_canadian_restrictions_cover_alberta(slug):
    review = eligibility(slug)
    assert review["public_status"] == "CANADA_RESTRICTED"
    assert review["alberta_status"] == "RESTRICTED"
    assert review["sources"]


@pytest.mark.parametrize("slug", ["bitfinex", "kucoin", "poloniex"])
def test_provincial_evidence_does_not_imply_alberta_eligibility(slug):
    review = eligibility(slug)
    assert review["public_status"] == "REGIONAL_RESTRICTIONS"
    assert review["alberta_status"] == "UNVERIFIED"


def test_canadian_service_cessation_is_separate_from_regional_restrictions():
    review = eligibility("binance")
    assert (
        review["public_status"] == review["alberta_status"] == "CANADIAN_SERVICE_CEASED"
    )
    assert "2023-09-25" in review["scope"]
    assert any("fintrac-canafe.canada.ca" in item["url"] for item in review["sources"])


@pytest.mark.parametrize("slug", ["bitbank", "luno"])
def test_resident_onboarding_limits_are_scoped(slug):
    review = eligibility(slug)
    assert review["alberta_status"] == "RESIDENT_ONBOARDING_UNAVAILABLE"
    assert review["public_status"] == "RESIDENT_ONBOARDING_UNAVAILABLE"
    assert review["sources"]


def test_asset_restrictions_and_unknown_status_preserve_evidence():
    kraken = eligibility("kraken")
    assert "USDT" in kraken["restricted_assets"]
    kraken["sources"].clear()
    assert eligibility("kraken")["sources"]
    unknown = eligibility("synthetic-unreviewed")
    assert unknown["public_status"] == unknown["alberta_status"] == "UNVERIFIED"
    assert unknown["sources"] == []
    assert unknown["reviewed_at"] is None


@pytest.mark.asyncio
async def test_exchanges_api_discloses_current_evidence_over_retained_snapshot(
    monkeypatch,
):
    retained = SimpleNamespace(
        id="synthetic-retained-okx",
        slug="okx",
        name="Synthetic exchange row",
        enabled=True,
        capabilities={
            "spot_supported": True,
            "canadian_eligibility": {"classification": "OLD"},
        },
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(collector_http=None))
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(retained, None)]))
    )
    monkeypatch.setattr(api, "collector_state", AsyncMock(return_value=(None, False)))

    (item,) = await api.exchanges(request, db, user=None)

    assert (
        item["capabilities"]["canadian_eligibility"]["public_status"]
        == "CANADA_RESTRICTED"
    )
    assert item["capabilities"]["spot_supported"] is True
    assert item["manual_trading_eligible"] is False
    assert retained.capabilities["canadian_eligibility"]["classification"] == "OLD"
