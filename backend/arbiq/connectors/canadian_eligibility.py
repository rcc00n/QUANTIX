"""Dated public jurisdiction evidence for spot adapter products."""

from dataclasses import dataclass
from enum import StrEnum

CSA_SOURCE = (
    "https://www.securities-administrators.ca/crypto-platforms-regulation-and-enforcement-actions/"
    "crypto-platforms-authorized-to-do-business-with-canadians/"
)
REVIEWED_AT = "2026-11-24"
REGISTERED_ENTITIES = {
    "kraken": "Payward Canada Inc.",
    "ndax": "Ndax Canada Inc.",
    "coinbase": "Coinbase Canada Inc.",
    "coinbaseexchange": "Coinbase Canada Inc.",
    "cryptocom": "Foris DAX CAN ULC",
}
OWNER_CHECKLIST = (
    "Confirm the contracting entity and its current registration for your province.",
    "Confirm your residency, identity verification and account permissions with the venue.",
    "Confirm the exact spot pair, order type, funding currencies and any API permissions.",
    "Verify account-specific fees, limits and custody or transfer restrictions separately.",
)


class PublicStatus(StrEnum):
    CANADIAN_SPOT_DOCUMENTED = "CANADIAN_SPOT_DOCUMENTED"
    SEPARATE_PRODUCT_UNVERIFIED = "SEPARATE_PRODUCT_UNVERIFIED"
    CANADA_RESTRICTED = "CANADA_RESTRICTED"
    CANADIAN_SERVICE_CEASED = "CANADIAN_SERVICE_CEASED"
    RESIDENT_ONBOARDING_UNAVAILABLE = "RESIDENT_ONBOARDING_UNAVAILABLE"
    REGIONAL_RESTRICTIONS = "REGIONAL_RESTRICTIONS"
    UNVERIFIED = "UNVERIFIED"


class AlbertaStatus(StrEnum):
    PUBLIC_SCOPE_DOCUMENTED = "PUBLIC_SCOPE_DOCUMENTED"
    RESTRICTED = "RESTRICTED"
    CANADIAN_SERVICE_CEASED = "CANADIAN_SERVICE_CEASED"
    RESIDENT_ONBOARDING_UNAVAILABLE = "RESIDENT_ONBOARDING_UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class Review:
    status: PublicStatus
    alberta: AlbertaStatus
    scope: str
    sources: tuple[tuple[str, str], ...] = ()
    restricted_assets: tuple[str, ...] = ()


_UNVERIFIED = Review(
    PublicStatus.UNVERIFIED,
    AlbertaStatus.UNVERIFIED,
    "Canada and Alberta spot access for this adapter product require public evidence.",
)

_REVIEWED = {
    "kraken": Review(
        PublicStatus.CANADIAN_SPOT_DOCUMENTED,
        AlbertaStatus.PUBLIC_SCOPE_DOCUMENTED,
        "Canadian spot registration covers Alberta. Kraken publishes asset and account limits "
        "for Canadian clients.",
        (
            (
                "Kraken jurisdiction and asset restrictions",
                "https://support.kraken.com/ca/articles/where-is-kraken-licensed-or-regulated",
            ),
            (
                "Payward Canada registration decision",
                "https://www.securities-administrators.ca/wp-content/uploads/2025/04/"
                "Payward-Canada-decision-document-signed-dated-April-1-2025.pdf",
            ),
        ),
        ("USDT",),
    ),
    "ndax": Review(
        PublicStatus.CANADIAN_SPOT_DOCUMENTED,
        AlbertaStatus.PUBLIC_SCOPE_DOCUMENTED,
        "The Canadian crypto trading decision covers Alberta and sets client and product terms.",
        (
            (
                "Ndax Canada registration decision",
                "https://www.securities-administrators.ca/wp-content/uploads/2024/12/"
                "Ndax-Canada-Inc.pdf",
            ),
        ),
    ),
    "coinbase": Review(
        PublicStatus.CANADIAN_SPOT_DOCUMENTED,
        AlbertaStatus.PUBLIC_SCOPE_DOCUMENTED,
        "Coinbase publishes Canadian retail spot terms and Alberta client requirements. "
        "Each account and pair remains subject to verification.",
        (
            (
                "Coinbase Canada user agreement",
                "https://www.coinbase.com/legal/user_agreement/canada",
            ),
            (
                "Coinbase Canadian regulatory FAQ",
                "https://help.coinbase.com/en-ca/coinbase/other-topics/legal-policies/"
                "canada-regulatory-faq",
            ),
        ),
    ),
    "coinbaseexchange": Review(
        PublicStatus.SEPARATE_PRODUCT_UNVERIFIED,
        AlbertaStatus.UNVERIFIED,
        "The Canadian entity and retail terms are documented. This adapter uses the separate "
        "Coinbase Exchange product, whose Canadian and Alberta account scope needs verification.",
        (
            (
                "Coinbase Canada retail agreement",
                "https://www.coinbase.com/legal/user_agreement/canada",
            ),
        ),
    ),
    "cryptocom": Review(
        PublicStatus.SEPARATE_PRODUCT_UNVERIFIED,
        AlbertaStatus.UNVERIFIED,
        "Foris DAX CAN ULC is listed by the CSA. Crypto.com documents retail limits for Alberta. "
        "Access through the global Exchange API needs separate evidence.",
        (
            (
                "Crypto.com Canadian account limits",
                "https://help.crypto.com/en/articles/9070282-net-buy-limit-and-client-loss-limits-"
                "for-canadian-customers-faq",
            ),
            (
                "Crypto.com Exchange spot restrictions",
                "https://help.crypto.com/en/articles/6320975-spot-trading-geo-restrictions",
            ),
        ),
    ),
    "okx": Review(
        PublicStatus.CANADA_RESTRICTED,
        AlbertaStatus.RESTRICTED,
        "OKX lists Canada among its restricted locations.",
        (
            (
                "OKX risk and compliance disclosure",
                "https://www.okx.com/help/risk-compliance-disclosure",
            ),
        ),
    ),
    "bybit": Review(
        PublicStatus.CANADA_RESTRICTED,
        AlbertaStatus.RESTRICTED,
        "Bybit lists Canada among its excluded jurisdictions.",
        (
            (
                "Bybit service-restricted countries",
                "https://www.bybit.com/en/help-center/article/Service-Restricted-Countries",
            ),
        ),
    ),
    "bitget": Review(
        PublicStatus.CANADA_RESTRICTED,
        AlbertaStatus.RESTRICTED,
        "Bitget lists Canada among its prohibited countries.",
        (("Bitget terms of use", "https://www.bitget.com/terms/legal/360014944032"),),
    ),
    "mexc": Review(
        PublicStatus.CANADA_RESTRICTED,
        AlbertaStatus.RESTRICTED,
        "MEXC's region guide lists Canada among its prohibited jurisdictions.",
        (
            (
                "MEXC restricted-country guide",
                "https://www.mexc.com/learn/article/"
                "mexc-restricted-countries-complete-list-of-prohibited-limited-regions/1",
            ),
        ),
    ),
    "gate": Review(
        PublicStatus.CANADA_RESTRICTED,
        AlbertaStatus.RESTRICTED,
        "Gate's global user agreement lists Canada among its restricted locations.",
        (("Gate global user agreement", "https://www.gate.com/legal/user-agreement"),),
    ),
    "binance": Review(
        PublicStatus.CANADIAN_SERVICE_CEASED,
        AlbertaStatus.CANADIAN_SERVICE_CEASED,
        "FINTRAC records Binance's exit from Canada on 2023-09-25. The current CSA platform "
        "list does not show a return to Canadian service.",
        (
            (
                "FINTRAC notice documenting Binance's Canadian exit",
                "https://fintrac-canafe.canada.ca/pen/amps/pen-2024-05-09-eng",
            ),
            ("Current CSA list of authorized crypto platforms", CSA_SOURCE),
            (
                "Binance undertaking to the OSC",
                "https://www.osc.ca/sites/default/files/2022-03/"
                "20220316-Binance-Undertaking-and-Acknowledgement.pdf",
            ),
        ),
    ),
    "kucoin": Review(
        PublicStatus.REGIONAL_RESTRICTIONS,
        AlbertaStatus.UNVERIFIED,
        "KuCoin's terms name Ontario and British Columbia among restricted locations. Alberta "
        "access requires separate evidence.",
        (("KuCoin terms of use", "https://www.kucoin.com/legal/terms-of-use"),),
    ),
    "bitfinex": Review(
        PublicStatus.REGIONAL_RESTRICTIONS,
        AlbertaStatus.UNVERIFIED,
        "Bitfinex publishes Ontario client restrictions. Alberta access needs separate evidence.",
        (
            (
                "Bitfinex Ontario client FAQ",
                "https://support.bitfinex.com/hc/en-us/articles/"
                "4416062608537-Ontario-Canada-Customers-on-Bitfinex-Frequently-Asked-Questions-FAQ",
            ),
        ),
    ),
    "poloniex": Review(
        PublicStatus.REGIONAL_RESTRICTIONS,
        AlbertaStatus.UNVERIFIED,
        "Poloniex published an Ontario trading halt. Alberta access needs separate evidence.",
        (
            (
                "Poloniex Ontario notice",
                "https://www.poloniex.com/announcement/4418301091479",
            ),
        ),
    ),
    "gemini": Review(
        PublicStatus.UNVERIFIED,
        AlbertaStatus.UNVERIFIED,
        "Gemini's published exchange-availability list omits Canada. Canadian and Alberta "
        "access remains unverified.",
        (("Gemini exchange availability", "https://www.gemini.com/areas-of-availability"),),
    ),
    "bitstamp": Review(
        PublicStatus.UNVERIFIED,
        AlbertaStatus.UNVERIFIED,
        "Bitstamp has published separate Canadian restrictions for Earn products. Canadian "
        "and Alberta access for this spot adapter remains unverified.",
        (
            (
                "Bitstamp product availability",
                "https://blog.bitstamp.net/post/the-new-bitstamp-app-goes-global/",
            ),
        ),
    ),
    "bitso": Review(
        PublicStatus.UNVERIFIED,
        AlbertaStatus.UNVERIFIED,
        "Bitso states that local availability varies by entity and account. Canadian and Alberta "
        "retail spot access for this adapter remains unverified.",
        (("Bitso product availability disclosures", "https://bitso.com/"),),
    ),
    "luno": Review(
        PublicStatus.RESIDENT_ONBOARDING_UNAVAILABLE,
        AlbertaStatus.RESIDENT_ONBOARDING_UNAVAILABLE,
        "Luno's list of supported countries for resident account sign-up excludes Canada.",
        (
            (
                "Luno supported countries of residence",
                "https://guide.luno.com/hc/en-gb/articles/"
                "11035628803101-Which-countries-can-I-use-my-Luno-account-in",
            ),
        ),
    ),
    "bitbank": Review(
        PublicStatus.RESIDENT_ONBOARDING_UNAVAILABLE,
        AlbertaStatus.RESIDENT_ONBOARDING_UNAVAILABLE,
        "Bitbank's published requirements for individual accounts specify Japanese residence.",
        (
            (
                "Bitbank residency requirements (Japanese)",
                "https://support.bitbank.cc/hc/ja/articles/360005259814",
            ),
        ),
    ),
}


def eligibility(exchange_id: str) -> dict:
    review = _REVIEWED.get(exchange_id, _UNVERIFIED)
    entity = REGISTERED_ENTITIES.get(exchange_id)
    sources = list(review.sources)
    if entity:
        sources.insert(0, ("CSA list of authorized crypto platforms", CSA_SOURCE))
    return {
        "classification": "REGISTERED_ENTITY" if entity else "UNVERIFIED",
        "entity": entity,
        "public_status": review.status.value,
        "alberta_status": review.alberta.value,
        "account_status": "UNVERIFIED",
        "manual_trading_eligible": False,
        "source": sources[0][1] if sources else None,
        "sources": [{"label": label, "url": url} for label, url in sources],
        "reviewed_at": REVIEWED_AT if sources else None,
        "scope": review.scope,
        "restricted_assets": list(review.restricted_assets),
        "owner_checklist": list(OWNER_CHECKLIST),
        "product_verified": False,
        "venue_group": "coinbase" if exchange_id == "coinbaseexchange" else exchange_id,
    }
