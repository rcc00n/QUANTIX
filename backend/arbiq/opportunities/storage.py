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
from arbiq.finance.fees import public_fee_tiers
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

    async def fee_version(self, db, market, exchange_id, now, rate, evidence=None):
        maker_rate = rate
        if evidence:
            source = evidence["version"]
            public_maker_rate = evidence.get("public_maker_rate")
            if public_maker_rate is not None:
                maker_rate = Decimal(public_maker_rate)
            assumptions = {
                **evidence,
                "verified": False,
                "currency": evidence["applied_fee_currency"],
                "maker_not_used": True,
                "maker_rate_placeholder": public_maker_rate is None,
                "rate_policy": "max(configured conservative floor, advertised taker, public taker)",
                "public_tiers": public_fee_tiers(evidence.get("public_schedule_version")),
            }
        else:
            source = "quantix.conservative_public_taker.v1"
            assumptions = {
                "verified": False,
                "currency": "quote_assumed",
                "advertised_rate": market.metadata.get("advertised_taker_rate"),
                "rate_policy": "max(configured conservative estimate, advertised public taker)",
                "account_tier_verified": False,
                "maker_not_used": True,
            }

        # Lookup immutable rows in this transaction. Caching IDs from an uncommitted
        # write poisons subsequent attempts if the transaction later rolls back.
        # The bounded scanner write budget keeps these two assumption lookups modest.
        row = await db.scalar(
            select(FeeVersion)
            .where(
                FeeVersion.market_id == market.id,
                FeeVersion.source == source,
                FeeVersion.taker_rate == rate,
                FeeVersion.assumptions == assumptions,
                FeeVersion.effective_at <= now,
            )
            .order_by(FeeVersion.effective_at.desc())
            .limit(1)
        )
        if row is None:
            row = FeeVersion(
                exchange_id=exchange_id,
                market_id=market.id,
                effective_at=now,
                maker_rate=maker_rate,
                taker_rate=rate,
                source=source,
                assumptions=assumptions,
            )
            db.add(row)
            await db.flush()
        return row.id

    async def snapshot(self, db, book):
        row = await db.scalar(
            select(OrderBookSnapshot)
            .where(
                OrderBookSnapshot.market_id == book.market_id,
                OrderBookSnapshot.content_sha256 == book.content_sha256,
                OrderBookSnapshot.received_at == book.received_at,
            )
            .limit(1)
        )
        if row is None:
            row = OrderBookSnapshot(
                market_id=book.market_id,
                observed_at=book.normalized.observed_at,
                received_at=book.received_at,
                bids=[[str(x.price), str(x.quantity)] for x in book.normalized.bids],
                asks=[[str(x.price), str(x.quantity)] for x in book.normalized.asks],
                sequence=book.sequence,
                source=book.source,
                quality=book.state.value,
                content_sha256=book.content_sha256,
            )
            db.add(row)
            await db.flush()
        return row.id

    async def save(
        self,
        key,
        payload,
        buy,
        sell,
        buy_market,
        sell_market,
        buy_exchange_id,
        sell_exchange_id,
        buy_fee,
        sell_fee,
        now,
    ):
        async with self.sessions() as db:
            route = await db.scalar(select(ArbitrageRoute).where(ArbitrageRoute.route_key == key))
            new = route is None
            old = route.status if route else None
            if new:
                route = ArbitrageRoute(
                    route_key=key,
                    buy_market_id=buy.market_id,
                    sell_market_id=sell.market_id,
                    run_id=self.run_id,
                    status="DETECTED",
                    actionable=False,
                    detected_at=now,
                    updated_at=now,
                    expires_at=payload["expires_at"],
                    revision=0,
                    payload={},
                )
                db.add(route)
                await db.flush()
                db.add(
                    ArbitrageLifecycleEvent(
                        route_id=route.id,
                        event_at=now,
                        status="DETECTED",
                        reason="Positive observed depth spread; no execution implied",
                        details={},
                    )
                )
            route.status = payload["status"]
            route.revision += 1
            route.run_id = self.run_id
            route.updated_at = now
            route.expires_at = payload["expires_at"]
            body = safe_json(
                {
                    **payload,
                    "id": str(route.id),
                    "detected_at": route.detected_at,
                    "updated_at": now,
                }
            )
            route.payload = body
            buy_id = await self.snapshot(db, buy)
            sell_id = await self.snapshot(db, sell)
            fee_evidence = payload.get("evidence", {}).get("fee_assumptions", {})
            bf = await self.fee_version(
                db, buy_market, buy_exchange_id, now, buy_fee, fee_evidence.get("buy")
            )
            sf = await self.fee_version(
                db, sell_market, sell_exchange_id, now, sell_fee, fee_evidence.get("sell")
            )
            evidence = {
                **body.get("evidence", {}),
                "buy_book": buy.model_dump(mode="json"),
                "sell_book": sell.model_dump(mode="json"),
                "buy_market_metadata": buy_market.metadata,
                "sell_market_metadata": sell_market.metadata,
                "buy_snapshot_id": str(buy_id),
                "sell_snapshot_id": str(sell_id),
                "buy_fee_version_id": str(bf),
                "sell_fee_version_id": str(sf),
            }
            db.add(
                ArbitrageEvaluation(
                    route_id=route.id,
                    revision=route.revision,
                    evaluated_at=now,
                    buy_snapshot_id=buy_id,
                    sell_snapshot_id=sell_id,
                    buy_fee_version_id=bf,
                    sell_fee_version_id=sf,
                    quantity=numeric(body.get("quantity")),
                    acquisition_cost_cad=numeric(body.get("acquisition_cost_cad")),
                    gross_profit_cad=numeric(body.get("costs", {}).get("gross_profit_cad")),
                    net_profit_cad=numeric(body.get("net_profit_cad")),
                    net_return_pct=numeric(body.get("net_return_pct")),
                    calculation_version=VERSION,
                    evidence=evidence,
                    result=body,
                )
            )
            if old != route.status:
                db.add(
                    ArbitrageLifecycleEvent(
                        route_id=route.id,
                        event_at=now,
                        status=route.status,
                        reason=", ".join(body["reasons"]) or "Market spread confirmed",
                        details={"revision": route.revision},
                    )
                )
            await db.commit()
            return body

    async def transition(self, route_id: UUID, status, reason, now):
        async with self.sessions() as db:
            route = await db.get(ArbitrageRoute, route_id)
            if not route or route.status == status:
                return
            route.status = status
            route.updated_at = now
            route.payload = {
                **route.payload,
                "status": status,
                "updated_at": now.isoformat(),
                "actionable": False,
                "reasons": [reason],
            }
            db.add(
                ArbitrageLifecycleEvent(
                    route_id=route.id, event_at=now, status=status, reason=reason, details={}
                )
            )
            await db.commit()

    async def expire_previous_runs(self, now):
        async with self.sessions() as db:
            rows = (
                await db.scalars(
                    select(ArbitrageRoute).where(
                        ArbitrageRoute.run_id != self.run_id,
                        ArbitrageRoute.status.not_in(["INVALIDATED", "EXPIRED"]),
                    )
                )
            ).all()
            for route in rows:
                route.status = "EXPIRED"
                route.updated_at = now
                route.payload = {
                    **route.payload,
                    "status": "EXPIRED",
                    "actionable": False,
                    "updated_at": now.isoformat(),
                    "reasons": ["collector_restarted_requires_new_evidence"],
                }
                db.add(
                    ArbitrageLifecycleEvent(
                        route_id=route.id,
                        event_at=now,
                        status="EXPIRED",
                        reason="collector_restarted_requires_new_evidence",
                        details={},
                    )
                )
            await db.commit()
