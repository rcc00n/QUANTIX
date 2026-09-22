"""Research persistence with immutable evidence, revisions and lifecycle transitions."""

from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import select

from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    FeeVersion,
    OrderBookSnapshot,
)
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.contracts import CALCULATION_VERSION

VERSION = CALCULATION_VERSION


def numeric(value):
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 78
        value = Decimal(value).quantize(Decimal("0.000000000000000001"))
        if not value.is_finite() or abs(value) >= Decimal("1e20"):
            raise ValueError("Financial evidence exceeds NUMERIC(38,18)")
        return value


class OpportunityStore:
    def __init__(self, sessions, run_id):
        self.sessions, self.run_id = sessions, run_id

    async def fee_version(self, db, market, exchange_id, now, rate):
        # Lookup immutable rows in this transaction. Caching IDs from an uncommitted
        # write poisons subsequent attempts if the transaction later rolls back.
        # The bounded scanner write budget keeps these two assumption lookups modest.
        row = await db.scalar(
            select(FeeVersion).where(
                FeeVersion.market_id == market.id,
                FeeVersion.source == "quantix.conservative_public_taker.v1",
                FeeVersion.taker_rate == rate,
            )
        )
        if row is None:
            row = FeeVersion(
                exchange_id=exchange_id,
                market_id=market.id,
                effective_at=now,
                maker_rate=rate,
                taker_rate=rate,
                source="quantix.conservative_public_taker.v1",
                assumptions={
                    "verified": False,
                    "currency": "quote_assumed",
                    "advertised_rate": market.metadata.get("advertised_taker_rate"),
                    "rate_policy": "max(configured conservative estimate, advertised public taker)",
                    "account_tier_verified": False,
                    "maker_not_used": True,
                },
            )
            db.add(row)
            await db.flush()
        return row.id

