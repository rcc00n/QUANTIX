"""Causal, versioned spot fee evidence used by paper research."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

FeeCurrency = Literal["base", "quote"]
POLICY_VERSION = "quantix.conservative_public_taker.v2"
PUBLIC_REVIEW_MAX_AGE = timedelta(days=90)
REVIEWED_AT = datetime(2026, 11, 24, 5, 49, tzinfo=UTC)


class FeeEvidenceStatus(StrEnum):
    ACCOUNT_VERIFIED = "ACCOUNT_VERIFIED"
    PUBLIC_SCHEDULE_VERIFIED = "PUBLIC_SCHEDULE_VERIFIED"
    CONSERVATIVE_ESTIMATE = "CONSERVATIVE_ESTIMATE"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class FeeTier:
    name: str
    maker_rate: Decimal
    taker_rate: Decimal
    minimum_volume_30d: Decimal | None = None
    minimum_asset_balance: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PublicFeeSchedule:
    version: str
    source_url: str
    scope: str
    tiers: tuple[FeeTier, ...]
    limitations: str
    verified_at: datetime = REVIEWED_AT
    effective_on: date | None = None
    buy_currency: FeeCurrency | None = None
    sell_currency: FeeCurrency | None = None
    minimum_commission_rule: str | None = None
    supplementary_sources: tuple[str, ...] = ()
    tier_unit: str = "USD"

    @property
    def standard_tier(self) -> FeeTier | None:
        # Data authors place the publicly documented non-discount tier first.
        return self.tiers[0] if self.tiers else None

    def tier_evidence(self) -> list[dict]:
        def decimal_text(value):
            return str(value) if value is not None else None

        return [
            {
                "tier": tier.name,
                "maker_rate": str(tier.maker_rate),
                "taker_rate": str(tier.taker_rate),
                "minimum_volume_30d": decimal_text(tier.minimum_volume_30d),
                "minimum_asset_balance": decimal_text(tier.minimum_asset_balance),
                "threshold_unit": self.tier_unit,
            }
            for tier in self.tiers
        ]


@dataclass(frozen=True, slots=True)
class FeeAssessment:
    applied_taker_rate: Decimal
    configured_taker_floor: Decimal
    advertised_taker_rate: Decimal | None
    evidence_status: FeeEvidenceStatus
    schedule: PublicFeeSchedule | None

    @property
    def version(self) -> str:
        suffix = self.schedule.version if self.schedule else "no-causal-public-schedule"
        return f"{POLICY_VERSION}/{suffix}/{self.evidence_status.value}"

    def currency(self, side: Literal["buy", "sell"]) -> FeeCurrency:
        schedule = self.schedule
        if schedule is None:
            return "quote"
        public_currency = schedule.buy_currency if side == "buy" else schedule.sell_currency
        return public_currency or "quote"

    def as_evidence(self, side: Literal["buy", "sell"] | None = None) -> dict:
        schedule = self.schedule
        tier = schedule.standard_tier if schedule else None
        return {
            "version": self.version,
            "policy_version": POLICY_VERSION,
            "fee_evidence_status": self.evidence_status.value,
            "pricing_classification": FeeEvidenceStatus.CONSERVATIVE_ESTIMATE.value,
            "account_tier_verified": False,
            "applied_taker_rate": str(self.applied_taker_rate),
            "configured_taker_floor": str(self.configured_taker_floor),
            "advertised_taker_rate": (
                str(self.advertised_taker_rate) if self.advertised_taker_rate is not None else None
            ),
            "public_schedule_version": schedule.version if schedule else None,
            "public_maker_rate": str(tier.maker_rate) if tier else None,
            "public_taker_rate": str(tier.taker_rate) if tier else None,
            "public_standard_tier": tier.name if tier else None,
            "scope": schedule.scope if schedule else None,
            "source_url": schedule.source_url if schedule else None,
            "supplementary_sources": list(schedule.supplementary_sources) if schedule else [],
            "verified_at": schedule.verified_at.isoformat() if schedule else None,
            "effective_on": (
                schedule.effective_on.isoformat() if schedule and schedule.effective_on else None
            ),
            "public_buy_fee_currency": schedule.buy_currency if schedule else None,
            "public_sell_fee_currency": schedule.sell_currency if schedule else None,
            "applied_fee_currency": self.currency(side) if side else None,
            "fee_currency_assumed": (
                not schedule
                or (schedule.buy_currency if side == "buy" else schedule.sell_currency) is None
            )
            if side
            else None,
            "public_minimum_commission_rule": schedule.minimum_commission_rule
            if schedule
            else None,
            "limitations": schedule.limitations if schedule else "No causal public fee schedule",
        }


def _tiers(rows: tuple[tuple, ...]) -> tuple[FeeTier, ...]:
    def optional_decimal(value):
        return Decimal(value) if value is not None else None

    return tuple(
        FeeTier(
            name,
            Decimal(maker),
            Decimal(taker),
            optional_decimal(volume),
            optional_decimal(balance[0]) if balance else None,
        )
        for name, volume, maker, taker, *balance in rows
    )


def _single(name: str, maker: str, taker: str) -> tuple[FeeTier, ...]:
    return (FeeTier(name, Decimal(maker), Decimal(taker)),)


KRAKEN_URL = "https://www.kraken.com/pro/fees"
KRAKEN_FEE_RULES = "https://support.kraken.com/articles/201893638-how-trading-fees-work-on-kraken"
KRAKEN_CURRENCY = (
    "Per-market fee precision determines the minimum charge. Fee asset is configurable."
)
COINBASE_URL = "https://help.coinbase.com/en/exchange/trading-and-funding/exchange-fees"
GEMINI_URL = "https://www.gemini.com/fees/activetrader-fee-schedule"
OKX_URL = "https://www.okx.com/en-gb/help/important-notice-consolidation-token-pairs"
STABLE_BASES = frozenset({"USDC", "USDT", "DAI", "PYUSD", "USDG", "EURC", "USDE", "GUSD", "RLUSD"})
FIAT_CODES = frozenset(
    {"CAD", "USD", "EUR", "GBP", "CHF", "JPY", "AUD", "TRY", "BRL", "AED", "SGD"}
)

# The selection uses only the first tier of an applicable revision. Other rows
# are retained as source evidence, never used as an inferred account discount.
PUBLIC_SCHEDULES: dict[tuple[str, str], PublicFeeSchedule] = {
    ("kraken", "spot"): PublicFeeSchedule(
        "kraken-pro-spot-2026-11-24",
        KRAKEN_URL,
        "Kraken Pro ordinary spot",
        _tiers(
            (
                ("1", 0, "0.004", "0.008"),
                ("2", 2500, "0.003", "0.006"),
                ("3", 10000, "0.0022", "0.0038", 20000),
                ("4", 25000, "0.002", "0.0035", 50000),
                ("5", 50000, "0.0015", "0.003", 100000),
                ("6", 100000, "0.0012", "0.0025", 200000),
                ("7", 250000, "0.001", "0.0022", 400000),
                ("8", 500000, "0.0008", "0.002", 600000),
                ("9", 1000000, "0.0006", "0.0018", 1000000),
                ("10", 2500000, "0.0004", "0.0015", 2500000),
                ("11", 5000000, "0.0002", "0.0012", 5000000),
                ("12", 10000000, "0", "0.001", 10000000),
                ("13", 50000000, "0", "0.0009", 20000000),
                ("14", 100000000, "0", "0.0008", 25000000),
                ("15", 250000000, "0", "0.0007", 50000000),
                ("16", 400000000, "0", "0.0006", 80000000),
                ("17", 500000000, "0", "0.0005", 100000000),
            )
        ),
        "Spot volume, futures volume, or eligible assets qualify for lower account tiers. "
        "Pair promotions and futures thresholds are not selected.",
        minimum_commission_rule=KRAKEN_CURRENCY,
        supplementary_sources=(KRAKEN_FEE_RULES,),
    ),
    ("kraken", "conversion"): PublicFeeSchedule(
        "kraken-pro-stablecoin-fx-2026-11-24",
        KRAKEN_URL,
        "Kraken Pro stablecoin-base and FX",
        _tiers(
            (
                ("standard", 0, "0.002", "0.002"),
                ("50K", 50000, "0.0016", "0.0016"),
                ("100K", 100000, "0.0012", "0.0012"),
                ("250K", 250000, "0.0008", "0.0008"),
                ("500K", 500000, "0.0004", "0.0004"),
                ("1M", 1000000, "0.0002", "0.0002"),
                ("10M", 10000000, "0", "0.0001"),
                ("100M", 100000000, "0", "0.00001"),
            )
        ),
        "Stablecoin-quote-only crypto books use ordinary spot. Special pair discounts "
        "are excluded.",
        minimum_commission_rule=KRAKEN_CURRENCY,
        supplementary_sources=(KRAKEN_FEE_RULES,),
    ),
    ("coinbaseexchange", "spot"): PublicFeeSchedule(
        "coinbase-exchange-spot-2026-11-24",
        COINBASE_URL,
        "Coinbase Exchange regular spot",
        _tiers(
            (
                ("standard", 0, "0.004", "0.006"),
                ("10K", 10000, "0.0025", "0.004"),
                ("50K", 50000, "0.0015", "0.0025"),
                ("100K", 100000, "0.001", "0.002"),
                ("1M", 1000000, "0.0008", "0.0018"),
                ("15M", 15000000, "0.0006", "0.0016"),
                ("75M", 75000000, "0.0003", "0.001"),
                ("250M", 250000000, "0", "0.0006"),
                ("400M", 400000000, "0", "0.0004"),
            )
        ),
        "Ordinary 30-day USD volume tiers outside the account-specific Liquidity Program.",
    ),
    ("coinbaseexchange", "stablepair"): PublicFeeSchedule(
        "coinbase-exchange-stablepair-2026-11-24",
        COINBASE_URL,
        "Coinbase Exchange FX Stablepair",
        _tiers(
            (
                ("outside Liquidity Program", None, "0", "0.000045"),
                ("Liquidity Program 1", None, "0", "0.000010"),
                ("Liquidity Program 2", None, "0", "0.000015"),
                ("Liquidity Program 3", None, "0", "0.000020"),
                ("Liquidity Program 4", None, "0", "0.000030"),
            )
        ),
        "Requires the official fx_stablecoin product flag. USDT/USD and USDT/USDC are excluded.",
        supplementary_sources=("https://api.exchange.coinbase.com/products",),
    ),
    ("coinbase", "spot"): PublicFeeSchedule(
        "coinbase-advanced-account-dependent-2026-11-24",
        "https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees",
        "Coinbase Advanced spot",
        (),
        "Account sign-in is required for the numerical fee tier. Exchange schedules are distinct.",
    ),
    ("gemini", "spot"): PublicFeeSchedule(
        "gemini-activetrader-spot-2026-09-01",
        GEMINI_URL,
        "Gemini ActiveTrader and exchange API",
        _tiers(
            (
                ("standard", 0, "0.006", "0.012", 0),
                ("10K", 10000, "0.004", "0.008", 1000000),
                ("25K", 25000, "0.0025", "0.005", 5000000),
                ("75K", 75000, "0.00125", "0.0025", 15000000),
                ("250K", 250000, "0.00075", "0.0015", 25000000),
                ("500K", 500000, "0.0006", "0.00125"),
                ("1M", 1000000, "0.0005", "0.001"),
                ("5M", 5000000, "0.0004", "0.00085"),
                ("10M", 10000000, "0.00025", "0.00065"),
                ("20M", 20000000, "0.0001", "0.0005"),
                ("50M", 50000000, "0", "0.00035"),
                ("100M", 100000000, "0", "0.00025"),
                ("250M", 250000000, "0", "0.0002"),
            )
        ),
        "30-day volume or eligible assets can qualify for an account tier. Other stablecoin "
        "pair discounts require an independently verified product designation.",
        effective_on=date(2026, 9, 1),
        buy_currency="quote",
        sell_currency="quote",
    ),
    ("gemini", "listed_stablepair"): PublicFeeSchedule(
        "gemini-activetrader-explicit-stablepairs-2026-09-01",
        GEMINI_URL,
        "Gemini ActiveTrader RLUSD/USD, USDC/GUSD and GUSD/USD",
        _single("listed stablecoin pair", "0", "0"),
        "Only the three explicitly published products. Calculations retain the configured floor.",
        effective_on=date(2026, 9, 1),
        buy_currency="quote",
        sell_currency="quote",
    ),
    ("ndax", "spot"): PublicFeeSchedule(
        "ndax-canada-spot-2026-11-24",
        "https://ndax.io/en/fees",
        "Ndax Canada spot",
        _single("flat trade charge", "0.002", "0.002"),
        "Same published trade fee per side. Public terms charge it in the acquired asset.",
        buy_currency="base",
        sell_currency="quote",
        supplementary_sources=(
            "https://ndax.io/en/blog/article/how-does-ndax-s-0-20-trading-fee-work",
            "https://ndax.io/en/legal/account-agreements/relationship-disclosure-document",
        ),
    ),
    ("bitget", "spot"): PublicFeeSchedule(
        "bitget-spot-2026-11-24",
        "https://www.bitget.com/fee/",
        "Bitget spot",
        _tiers(
            (
                ("VIP0", 0, "0.001", "0.001", 0),
                ("VIP1", 500000, "0.0008", "0.0008", 30000),
                ("VIP2", 2000000, "0.00065", "0.0007", 50000),
                ("VIP3", 8000000, "0.0005", "0.0006", 250000),
                ("VIP4", 30000000, "0.0004", "0.0005", 750000),
            )
        ),
        "Selected public tiers. Tier qualification also considers daily and 30-day balance or BGB "
        "balance. Fee-token discounts, rebates and regional variation are excluded.",
        tier_unit="USDT",
    ),
    ("bitfinex", "spot"): PublicFeeSchedule(
        "bitfinex-zero-spot-2025-12-17",
        "https://support.bitfinex.com/hc/en-us/articles/213919589-What-fees-does-Bitfinex-charge",
        "Bitfinex spot",
        _single("published spot rate", "0", "0"),
        "Explicit published zero spot commission. The configured research floor still applies.",
        effective_on=date(2025, 12, 17),
    ),
    ("cryptocom", "spot"): PublicFeeSchedule(
        "cryptocom-exchange-spot-2026-11-24",
        "https://crypto.com/exchange/document/fees-limits?tab=2",
        "Crypto.com Exchange spot",
        _tiers(
            (
                ("1", 0, "0.0025", "0.005"),
                ("2", 10000, "0.002", "0.004"),
                ("3", 50000, "0.0015", "0.0025"),
                ("4", 250000, "0.001", "0.002"),
                ("5", 500000, "0.0008", "0.0018"),
            )
        ),
        "Exchange product only. CRO and institutional rates require distinct account evidence.",
    ),
    ("binance", "spot"): PublicFeeSchedule(
        "binance-global-spot-2026-11-24",
        "https://www.binance.com/en/fee/trading",
        "Binance global regular spot",
        _tiers(
            (
                ("regular", 0, "0.001", "0.001"),
                ("VIP1", 1000000, "0.0009", "0.001"),
                ("VIP2", 5000000, "0.0008", "0.001"),
                ("VIP3", 20000000, "0.0004", "0.0006"),
                ("VIP4", 75000000, "0.0004", "0.00052"),
                ("VIP5", 150000000, "0.00025", "0.00031"),
                ("VIP6", 400000000, "0.0002", "0.00029"),
                ("VIP7", 800000000, "0.00019", "0.00028"),
                ("VIP8", 2000000000, "0.00016", "0.00025"),
                ("VIP9", 4000000000, "0.00011", "0.00023"),
            )
        ),
        "Global schedule scope. BNB balance also controls VIP tier. BNB payment, USDC and "
        "promotional schedules are excluded. Canadian eligibility requires distinct evidence.",
    ),
    ("okx", "spot"): PublicFeeSchedule(
        "okx-spot-group-1-2026-08-14",
        OKX_URL,
        "OKX ordinary crypto/USDT spot, Group 1",
        _tiers(
            (
                ("regular", 0, "0.0008", "0.001", 0),
                ("VIP1", 1000000, "0.000675", "0.0008", 100000),
                ("VIP2", 5000000, "0.0006", "0.0007", 200000),
                ("VIP3", 10000000, "0.00055", "0.00065", 2000000),
                ("VIP4", 20000000, "0.0003", "0.00045", 5000000),
                ("VIP5", 100000000, "0.00025", "0.00035", 20000000),
                ("VIP6", 200000000, "0", "0.0003", 50000000),
                ("VIP7", 500000000, "-0.00002", "0.00025", 100000000),
                ("VIP8", 1000000000, "-0.00005", "0.0002", 250000000),
                ("VIP9", 5000000000, "-0.00005", "0.00015", 500000000),
            )
        ),
        "Named Group 1 scope. Other regional accounts, fiat, stablecoin and special groups "
        "can differ. Public rate gives no account eligibility or rebate.",
        effective_on=date(2026, 8, 14),
        buy_currency="base",
        sell_currency="quote",
        supplementary_sources=("https://www.okx.com/help/trading-fee-rules-faq",),
    ),
    ("bybit", "spot"): PublicFeeSchedule(
        "bybit-crypto-spot-2026-11-24",
        "https://www.bybit.com/en/help-center/article/Trading-Fee-Structure?category=top",
        "Bybit ordinary crypto-to-crypto spot",
        _tiers(
            (
                ("VIP0", None, "0.001", "0.001"),
                ("VIP1", None, "0.000675", "0.0008"),
                ("VIP2", None, "0.00065", "0.000775"),
                ("VIP3", None, "0.000625", "0.00075"),
                ("VIP4", None, "0.0005", "0.0006"),
                ("VIP5", None, "0.0004", "0.0005"),
                ("Supreme VIP", None, "0.0003", "0.00045"),
            )
        ),
        "Base rates for regular crypto-to-crypto spot. Region, account tier, special zones "
        "and fiat pairs have distinct rates.",
    ),
    ("bitso", "USD"): PublicFeeSchedule(
        "bitso-international-usdc-quote-2026-11-24",
        "https://bitso.com/fees?hsLang=es-mx",
        "Bitso International USDC digital dollar markets",
        _tiers(
            (
                ("standard", 0, "0.003", "0.0036"),
                ("1K", 1000, "0.00239", "0.00333"),
                ("5K", 5000, "0.00205", "0.00282"),
                ("10K", 10000, "0.00171", "0.00243"),
                ("50K", 50000, "0.00137", "0.00205"),
                ("100K", 100000, "0.00114", "0.00154"),
                ("1M", 1000000, "0.00085", "0.00095"),
                ("5M", 5000000, "0.00072", "0.00082"),
                ("10M", 10000000, "0.0006", "0.00069"),
                ("20M", 20000000, "0.0005", "0.0006"),
                ("30M", 30000000, "0.0004", "0.0005"),
            )
        ),
        "Bitso labels these USDC markets USD for the fee schedule. That label provides "
        "no conversion evidence or asset identity for the book.",
    ),
    ("bitso", "MXN"): PublicFeeSchedule(
        "bitso-international-mxn-quote-2026-11-24",
        "https://bitso.com/fees?hsLang=es-mx",
        "Bitso International MXN spot",
        _tiers(
            (
                ("standard", 0, "0.006", "0.0078"),
                ("20K", 20000, "0.0059", "0.0076"),
                ("100K", 100000, "0.0057", "0.00741"),
                ("500K", 500000, "0.0056", "0.00728"),
                ("1M", 1000000, "0.0055", "0.00715"),
                ("1.5M", 1500000, "0.005", "0.0065"),
                ("5M", 5000000, "0.0045", "0.00585"),
                ("50M", 50000000, "0.002", "0.0026"),
                ("150M", 150000000, "0.001", "0.0013"),
            )
        ),
        "Scope is MXN-quoted spot on Bitso International.",
        tier_unit="MXN",
    ),
}


def public_fee_tiers(version: str | None) -> list[dict]:
    schedule = next(
        (value for value in PUBLIC_SCHEDULES.values() if value.version == version), None
    )
    return schedule.tier_evidence() if schedule else []


def public_fee_schedule(
    adapter: str, base: str, quote: str, *, fx_stablecoin: bool = False
) -> PublicFeeSchedule | None:
    scope = "spot"
    if adapter == "kraken" and (
        base in STABLE_BASES or (base in FIAT_CODES and quote in FIAT_CODES)
    ):
        scope = "conversion"
    elif (
        adapter == "coinbaseexchange"
        and fx_stablecoin
        and (base, quote)
        not in {
            ("USDT", "USD"),
            ("USDT", "USDC"),
        }
    ):
        scope = "stablepair"
    elif adapter == "gemini" and (base, quote) in {
        ("RLUSD", "USD"),
        ("USDC", "GUSD"),
        ("GUSD", "USD"),
    }:
        scope = "listed_stablepair"
    elif adapter == "okx" and (quote != "USDT" or base in STABLE_BASES or base in FIAT_CODES):
        return None
    elif adapter == "bybit" and (base in FIAT_CODES or quote in FIAT_CODES):
        return None
    elif adapter == "bitso":
        scope = "USD" if quote == "USDC" else quote
    return PUBLIC_SCHEDULES.get((adapter, scope))


def assess_fee(
    adapter: str,
    base: str,
    quote: str,
    *,
    configured_taker_floor: Decimal,
    advertised_taker,
    now: datetime,
    fx_stablecoin: bool = False,
) -> FeeAssessment:
    if now.utcoffset() != timedelta(0):
        raise ValueError("Fee evidence requires UTC decision time")
    # CCXT metadata is normalized to a decimal string before this boundary.
    try:
        advertised = (
            Decimal(advertised_taker)
            if isinstance(advertised_taker, (str, Decimal, int))
            and not isinstance(advertised_taker, bool)
            else None
        )
        if advertised is not None and (not advertised.is_finite() or not 0 <= advertised < 1):
            advertised = None
    except (ValueError, ArithmeticError):
        advertised = None
    schedule = public_fee_schedule(adapter, base, quote, fx_stablecoin=fx_stablecoin)
    state = FeeEvidenceStatus.UNKNOWN
    public_taker = None
    if schedule and (
        schedule.verified_at > now or (schedule.effective_on and schedule.effective_on > now.date())
    ):
        schedule = None
    if schedule and schedule.standard_tier:
        public_taker = schedule.standard_tier.taker_rate
        state = (
            FeeEvidenceStatus.STALE
            if now - schedule.verified_at > PUBLIC_REVIEW_MAX_AGE
            else FeeEvidenceStatus.PUBLIC_SCHEDULE_VERIFIED
        )
    # A stale public floor remains a conservative cost bound. Letting it expire
    # into a smaller modeled cost could incorrectly promote a research route.
    applied = max(
        value for value in (configured_taker_floor, advertised, public_taker) if value is not None
    )
    return FeeAssessment(applied, configured_taker_floor, advertised, state, schedule)
