"""Public schedules are reviewed source evidence. All test books are SYNTHETIC."""

from datetime import timedelta
from decimal import Decimal

import pytest
from arbiq.finance.fees import (
    PUBLIC_REVIEW_MAX_AGE,
    REVIEWED_AT,
    FeeEvidenceStatus,
    assess_fee,
    public_fee_tiers,
)
from arbiq.opportunities.analysis import load_input as read_market_input
from arbiq.opportunities.contracts import identity_cad
from test_arbitrage_engine import SyntheticCache, capture, engine_fixture

D = Decimal


def assessment(
    adapter, base="BTC", quote="USD", *, advertised=None, now=REVIEWED_AT, **kwargs
):
    return assess_fee(
        adapter,
        base,
        quote,
        configured_taker_floor=D("0.006"),
        advertised_taker=advertised,
        now=now,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("adapter", "public", "applied"),
    [
        ("kraken", "0.008", "0.008"),
        ("gemini", "0.012", "0.012"),
        ("bitfinex", "0", "0.006"),
        ("ndax", "0.002", "0.006"),
        ("coinbaseexchange", "0.006", "0.006"),
    ],
)
def test_public_rates_raise_the_conservative_floor_without_account_assumption(
    adapter, public, applied
):
    result = assessment(adapter)
    assert result.evidence_status == FeeEvidenceStatus.PUBLIC_SCHEDULE_VERIFIED
    assert result.applied_taker_rate == D(applied)
    evidence = result.as_evidence()
    assert evidence["public_taker_rate"] == public
    assert evidence["account_tier_verified"] is False
    assert (
        evidence["pricing_classification"]
        == FeeEvidenceStatus.CONSERVATIVE_ESTIMATE.value
    )
    assert evidence["verified_at"] == REVIEWED_AT.isoformat()
    assert (
        public_fee_tiers(evidence["public_schedule_version"])[0]["taker_rate"] == public
    )


def test_unknown_and_stale_sources_preserve_causal_cost_bounds():
    before_publication = assessment(
        "gemini", now=REVIEWED_AT - timedelta(microseconds=1)
    )
    assert before_publication.evidence_status == FeeEvidenceStatus.UNKNOWN
    assert before_publication.schedule is None
    assert before_publication.applied_taker_rate == D("0.006")
    stale = assessment(
        "gemini", now=REVIEWED_AT + PUBLIC_REVIEW_MAX_AGE + timedelta(seconds=1)
    )
    assert stale.evidence_status == FeeEvidenceStatus.STALE
    assert stale.applied_taker_rate == D("0.012")
    assert stale.version != assessment("gemini").version


@pytest.mark.parametrize(
    "advertised", [None, "NaN", "Infinity", "-0.1", "1", "n/a", 0.02, True]
)
def test_invalid_or_inexact_fee_metadata_is_not_priced_as_zero(advertised):
    result = assessment("unsupported", advertised=advertised)
    assert result.evidence_status == FeeEvidenceStatus.UNKNOWN
    assert result.advertised_taker_rate is None
    assert result.applied_taker_rate == D("0.006")


def test_metadata_with_a_higher_nonzero_cost_is_retained():
    result = assessment("kraken", advertised="0.01")
    assert result.applied_taker_rate == D("0.01")
    assert result.as_evidence()["public_taker_rate"] == "0.008"


def test_specific_pair_fees_derive_from_product_rules_and_flags():
    regular = assessment("kraken", "BTC", "USDT")
    conversion = assessment("kraken", "USDC", "USD")
    assert regular.applied_taker_rate == D("0.008")
    assert conversion.as_evidence()["public_taker_rate"] == "0.002"
    assert conversion.as_evidence()["public_minimum_commission_rule"]
    assert conversion.applied_taker_rate == D("0.006")
    assert (
        assessment("gemini", "USDC", "GUSD").as_evidence()["public_taker_rate"] == "0"
    )
    assert assessment("bitso", "BTC", "MXN").applied_taker_rate == D("0.0078")
    stable_pair = assessment("coinbaseexchange", "USDC", "USD", fx_stablecoin=True)
    assert stable_pair.as_evidence()["public_taker_rate"] == "0.000045"
    assert stable_pair.applied_taker_rate == D("0.006")
    assert (
        assessment("coinbaseexchange", "USDC", "USD").as_evidence()["public_taker_rate"]
        == "0.006"
    )
    assert (
        assessment("coinbaseexchange", "USDT", "USD", fx_stablecoin=True).as_evidence()[
            "public_taker_rate"
        ]
        == "0.006"
    )


def test_exchange_and_advanced_share_no_assumed_fee_schedule():
    advanced = assessment("coinbase", advertised="0.009", fx_stablecoin=True)
    assert advanced.evidence_status == FeeEvidenceStatus.UNKNOWN
    assert advanced.as_evidence()["scope"] == "Coinbase Advanced spot"
    assert advanced.as_evidence()["public_maker_rate"] is None
    assert advanced.applied_taker_rate == D("0.009")


@pytest.mark.asyncio
async def test_scanner_pins_public_denomination_and_rate_for_exact_replay():
    engine, buy, sell, _ = engine_fixture(buy_slug="ndax", sell_slug="gemini")
    engine.collector.utcnow = lambda: REVIEWED_AT
    engine.collector.cache = SyntheticCache(
        [
            capture("ndax", buy, received_at=REVIEWED_AT),
            capture("gemini", sell, ask="111", bid="110", received_at=REVIEWED_AT),
        ]
    )
    await engine.evaluate_pair(buy.id, sell.id, {"CAD": identity_cad()}, REVIEWED_AT)
    _, body, _, _ = engine.store.saved[0]
    evidence = body["evidence"]
    buy_fee = read_market_input(evidence["calculation_inputs"]["buy"]).fee
    sell_fee = read_market_input(evidence["calculation_inputs"]["sell"]).fee
    assert (
        buy_fee.currency
        == evidence["fee_assumptions"]["buy"]["applied_fee_currency"]
        == "base"
    )
    assert buy_fee.rate == D("0.006") and not buy_fee.verified
    assert sell_fee.rate == D("0.012") and not sell_fee.verified
    assert sell_fee.version == evidence["fee_assumptions"]["sell"]["version"]
    assert body["actionable"] is False
