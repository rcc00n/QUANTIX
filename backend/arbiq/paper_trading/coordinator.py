"""Autonomous PAPER ONLY simulation task under the collector singleton lease.

First valid post-delay observations are pinned on arrival. Each leg commits its own
journal; delayed transfers cannot sell until settlement and a new observation.
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import func, select

from arbiq.core.clock import elapsed_seconds
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageRoute,
    Asset,
    FeeVersion,
    FXQuote,
    Market,
    PaperFill,
    PaperOrder,
    PaperSession,
    PaperTrade,
    SimulationEvent,
    User,
)
from arbiq.market_data.quality import CapturedBook, safe_json
from arbiq.opportunities.analysis import dump_input, load_input
from arbiq.opportunities.contracts import Costs, Fee
from arbiq.opportunities.math import convert, optimize_route
from arbiq.opportunities.storage import OpportunityStore
from arbiq.paper_trading.execution import (
    VERSION,
    evidence_eligibility,
    floor_derived,
    planned_reserves,
    recheck_projection,
    simulate_leg,
)
from arbiq.paper_trading.market import PaperMarkets
from arbiq.paper_trading.models import ExecutionObservation, PaperControl, RebalanceRequest
from arbiq.paper_trading.service import Allocation, create_portfolio
from arbiq.portfolio import accounting, analytics, valuation
from arbiq.portfolio.contracts import (
    AccountingError,
    InsufficientBalance,
    Reservation,
    decimal_context,
    stored,
)
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint, PortfolioPosition
from arbiq.settings.strategy import minimum_buy_satisfied, presets, read_pinned_strategy

log = structlog.get_logger()
ACTIVE = {"SCHEDULED", "RECHECKING", "EXECUTING"}
TERMINAL = {"COMPLETED", "PARTIAL", "MISSED", "FAILED", "EXPIRED"}


def allowed_paper_asset(allowed, ticker, canonical):
    """Keep legacy ticker filters and match canonical plan IDs against pinned evidence."""
    return not allowed or any((canonical if ":" in asset else ticker) == asset for asset in allowed)


def event(db, trade, status, now, reason="", details=None):
    trade.status = status
    trade.reason = reason or None
    if status in TERMINAL:
        trade.completed_at = now
    db.add(
        SimulationEvent(
            trade_id=trade.id,
            order_id=None,
            event_type=status,
            reason=reason or status,
            occurred_at=now,
            provenance=safe_json(details or {}),
        )
    )


def trade_dto(t):
    return {
        "id": str(t.id),
        "session_id": str(t.session_id),
        "route_id": str(t.route_id) if t.route_id else None,
        "evaluation_id": str(t.evaluation_id) if t.evaluation_id else None,
        "status": t.status,
        "classification": t.classification,
        "mode": t.mode,
        "requested_at": t.requested_at,
        "eligible_at": t.eligible_at,
        "completed_at": t.completed_at,
        "reason": t.reason,
        "requested_quantity": str(t.requested_quantity)
        if t.requested_quantity is not None
        else None,
        "reaction_delay_ms": t.reaction_delay_ms,
        "state": t.state,
        "assumptions": t.assumptions,
        "strategy_version_id": str(t.strategy_version_id) if t.strategy_version_id else None,
    }


class PaperCoordinator:
    def __init__(self, collector):
        self.collector = collector
        self.config = collector.settings.paper
        self.sessions = collector.store.sessions
        self.market = PaperMarkets(collector)
        self.targets = {}
        self.recent = defaultdict(lambda: deque(maxlen=16))
        self.store = OpportunityStore(self.sessions, collector.store.run_id)
        self.last_tick = 0.0
        self.last_valuation = 0.0
        self.metrics = defaultdict(int)
        self.daily_hours = {}

    def clock(self):
        return (
            self.collector.utc_clock.snapshot()
            if self.collector.utc_clock
            else {"trusted": False, "host_healthy": False}
        )

    async def observe(self, book):
        if book.market_id not in self.targets:
            return
        self.recent[book.market_id].append(book)
        clock = self.clock()
        for target in list(self.targets.get(book.market_id, ())):
            if book.received_at < target["eligible_at"]:
                continue
            if (
                target.get("raw_due")
                and target["run_id"] == str(self.collector.store.run_id)
                and elapsed_seconds() < target["raw_due"]
            ):
                continue
            strategy = read_pinned_strategy(target["settings"])
            allowed, reason = evidence_eligibility(
                book,
                now=self.collector.utcnow(),
                eligible_at=target["eligible_at"],
                maximum_age=strategy.maximum_book_age_seconds,
                research=target["research"],
                allow_missing=strategy.allow_missing_source_timestamp,
                minimum_confidence=strategy.minimum_confidence,
                clock_trusted=clock["trusted"],
            )
            if not allowed:
                self.metrics["rejected_observations"] += 1
                continue
            references = [
                b
                for b in self.recent.get(target.get("opposite_id"), ())
                if b.received_at <= book.received_at and b.ingestion_index <= book.ingestion_index
            ]
            opposite = references[-1].model_dump(mode="json") if references else None
            fx = self.collector.scanner.fx_routes(book.received_at)
            fx = {
                k: dump_input(v)
                for k, v in fx.items()
                if v.available_at is None or v.available_at <= book.received_at
            }
            async with self.sessions() as db:
                trade = await db.get(PaperTrade, target["trade_id"])
                order = await db.scalar(
                    select(PaperOrder).where(PaperOrder.id == target["order_id"]).with_for_update()
                )
                if (
                    not trade
                    or trade.status not in ACTIVE
                    or not order
                    or order.status != "pending"
                ):
                    continue
                if await db.scalar(
                    select(ExecutionObservation.id)
                    .where(ExecutionObservation.order_id == order.id)
                    .limit(1)
                ):
                    continue
                sid = await self.store.snapshot(db, book)
                # Captured here before awaiting future observations. No later FX
                # update is used to price an earlier execution.
                db.add(
                    ExecutionObservation(
                        trade_id=trade.id,
                        order_id=order.id,
                        snapshot_id=sid,
                        received_at=book.received_at,
                        source_at=book.exchange_timestamp,
                        payload={
                            "book": book.model_dump(mode="json"),
                            "clock": clock,
                            "fx": fx,
                            "first_valid_after_delay": True,
                            "opposite_book_asof": opposite,
                            "eligibility": reason,
                        },
                    )
                )
                await db.commit()
                self.metrics["causal_observations"] += 1

    @decimal_context
    async def bootstrap(self, db, now):
        if not self.config.bootstrap_portfolios:
            return
        user = await db.scalar(
            select(User).where(
                User.username == self.collector.settings.admin_username, User.active.is_(True)
            )
        )
        if not user:
            return
        existing = list(
            await db.scalars(
                select(PaperControl).join(PaperSession).where(PaperSession.user_id == user.id)
            )
        )
        if not any(x.classification == "VALIDATED" for x in existing):
            await create_portfolio(
                db,
                user.id,
                "Validated paper portfolio",
                presets()["Conservative"],
                Decimal(10000),
                None,
                now,
            )
        if not any(x.classification == "RESEARCH" for x in existing):
            # Independent counterfactual portfolio. Crypto consumes the same10k
            # budget at observed initialization marks; there are no free assets.
            allocations = []
            for slug in ("kraken", "ndax"):
                rt = self.collector.runtimes.get(slug)
                if not rt:
                    continue
                m = next(
                    (m for m in rt.markets if m.base.symbol == "BTC" and m.quote.symbol == "CAD"),
                    None,
                )
                if not m:
                    return
                model = self.market.markets.get(m.id)
                book = self.collector.cache.get(m.id, now=now)
                if not model or not book or book.state.value != "HEALTHY":
                    return
                quantity = Decimal(2500) / book.normalized.bids[0].price

                quantity = floor_derived(quantity, m.amount_step or Decimal("1e-18"))
                mark = self.market.mark(rt.exchange_id, model.base_asset_id, quantity, now, True)
                if mark.cad_per_unit is None:
                    return
                allocations.extend(
                    [
                        Allocation(
                            exchange=slug,
                            asset="asset:BTC",
                            quantity=quantity,
                            cad_rate=stored(mark.cad_per_unit),
                            evidence=mark.provenance,
                        ),
                        Allocation(
                            exchange=slug,
                            quantity=Decimal(5000) - stored(quantity * stored(mark.cad_per_unit)),
                        ),
                    ]
                )
            if len(allocations) == 4:
                await create_portfolio(
                    db,
                    user.id,
                    "Research shadow — independent capital",
                    presets()["Aggressive Research"],
                    Decimal(10000),
                    allocations,
                    now,
                    observed_funding=True,
                )
        await db.flush()

    @decimal_context
    async def schedule(self, db, control, session, route, evaluation, now):
        strategy = read_pinned_strategy(control.settings)
        key = f"paper:{session.id}:{evaluation.id}:{control.strategy_version_id}"
        await accounting.lock_session(db, session.id)
        if await db.scalar(select(PaperTrade.id).where(PaperTrade.idempotency_key == key)):
            return
        hour_count = await db.scalar(
            select(func.count())
            .select_from(PaperTrade)
            .where(
                PaperTrade.session_id == session.id,
                PaperTrade.requested_at >= now - timedelta(hours=1),
            )
        )
        if hour_count >= strategy.maximum_simulations_per_hour:
            self.metrics["hourly_deferred_candidates"] += 1
            return
        total_pending = await db.scalar(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status.in_(ACTIVE))
        )
        if total_pending >= self.config.maximum_pending:
            self.metrics["global_pending_deferred"] += 1
            return
        body = evaluation.result
        clock = self.clock()
        delay = strategy.reaction_delay_seconds
        uncertainty = (clock.get("uncertainty_ms") or 0) / 1000
        due = now + timedelta(seconds=delay + 2 * uncertainty)
        trade = PaperTrade(
            session_id=session.id,
            opportunity_id=None,
            route_id=route.id,
            evaluation_id=evaluation.id,
            strategy_version_id=control.strategy_version_id,
            idempotency_key=key,
            classification=control.classification,
            mode=strategy.mode,
            status="CANDIDATE",
            requested_at=now,
            reaction_delay_ms=delay * 1000,
            eligible_at=due,
            calculation_version=VERSION,
            assumptions={
                "strategy": strategy.model_dump(mode="json"),
                "classification": control.classification,
                "buy_exchange": body.get("buy_exchange"),
                "sell_exchange": body.get("sell_exchange"),
                "asset": body.get("base_asset"),
                "detection_evidence_id": str(evaluation.id),
                "modeled_profile_not_real_latency": True,
            },
            state={
                "clock": clock,
                "collector_run_id": str(self.collector.store.run_id),
                "raw_due": elapsed_seconds() + delay,
                "clock_uncertainty_buffer_seconds": str(2 * uncertainty),
            },
        )
        db.add(trade)
        await db.flush()
        event(db, trade, "CANDIDATE", now)
        event(db, trade, "ELIGIBILITY_CHECK", now)
        self.metrics["candidates"] += 1

        def reject(reason, details=None):
            event(db, trade, "MISSED", now, reason, details)
            self.metrics["ineligible"] += 1

        if control.classification == "VALIDATED" and (
            not route.actionable or not clock["host_healthy"]
        ):
            reject("VALIDATED_REQUIREMENTS_UNVERIFIED")
            return
        if not clock["trusted"]:
            reject("UTC_UNTRUSTED")
            return
        if evaluation.evaluated_at > now:
            reject("EVALUATION_NOT_AVAILABLE_AT_SCHEDULING_TIME")
            return
        if strategy.allowed_exchanges and any(
            body.get(k) not in strategy.allowed_exchanges for k in ("buy_exchange", "sell_exchange")
        ):
            reject("EXCHANGE_FILTER")
            return
        inputs = evaluation.evidence.get("calculation_inputs")
        inputs = inputs if isinstance(inputs, dict) else {}
        buy_input = inputs.get("buy")
        canonical_base = buy_input.get("base_currency") if isinstance(buy_input, dict) else None
        if not allowed_paper_asset(strategy.allowed_assets, body.get("base_asset"), canonical_base):
            reject("ASSET_FILTER")
            return
        active = await db.scalar(
            select(func.count())
            .select_from(PaperTrade)
            .where(PaperTrade.session_id == session.id, PaperTrade.status.in_(ACTIVE))
        )
        if active >= strategy.maximum_concurrent:
            reject("CONCURRENCY_LIMIT")
            return
        transfer = strategy.mode == "TRANSFER" or (
            strategy.mode == "RESEARCH_SHADOW" and strategy.research_execution_model == "TRANSFER"
        )
        if transfer and (
            not strategy.transfer.network
            or not strategy.transfer.withdrawal_enabled
            or not strategy.transfer.deposit_enabled
        ):
            reject("TRANSFER_NETWORK_OR_FUNDING_UNVERIFIED")
            return
        if (
            transfer
            and control.classification != "RESEARCH"
            and not (strategy.transfer.network_verified and strategy.transfer.costs_verified)
        ):
            reject("TRANSFER_COSTS_UNVERIFIED")
            return
        try:
            buy = load_input(inputs["buy"])
            sell = load_input(inputs["sell"])
            buy = replace(
                buy,
                fee=Fee(
                    max(buy.fee.rate, strategy.taker_fee_rate),
                    strategy.buy_fee_currency,
                    strategy.fee_verified,
                    "versioned paper strategy taker assumption",
                ),
            )
            sell = replace(
                sell,
                fee=Fee(
                    max(sell.fee.rate, strategy.taker_fee_rate),
                    strategy.sell_fee_currency,
                    strategy.fee_verified,
                    "versioned paper strategy taker assumption",
                ),
            )
            result = await asyncio.to_thread(
                optimize_route,
                buy,
                sell,
                budget_cad=floor_derived(
                    strategy.trade_amount_cad / (1 + strategy.slippage_bps / 10000),
                    Decimal("1e-18"),
                ),
                now=evaluation.evaluated_at,
                costs=Costs(
                    slippage_bps=strategy.slippage_bps, rebalancing_bps=strategy.rebalance_bps
                ),
                budgets=(),
                max_search_evaluations=128,
            )
            best = result.best
        except (ValueError, TypeError, KeyError, ArithmeticError):
            reject("CALCULATION_EVIDENCE_INCOMPLETE")
            return
        if best is None:
            reject("NO_FEASIBLE_FUNDED_CAD_SIZE")
            return
        if not minimum_buy_satisfied(strategy, best.acquisition_cost_cad):
            reject("MINIMUM_BUY_CAPITAL")
            return
        try:
            buy_reserve, sell_reserve = planned_reserves(
                best, buy.fee, sell.fee, strategy.slippage_bps
            )
        except (ValueError, ArithmeticError):
            reject("UNREPRESENTABLE_RESERVATION")
            return
        if (
            best.acquisition_cost_cad is None
            or best.acquisition_cost_cad < strategy.minimum_liquidity_cad
        ):
            reject("MINIMUM_LIQUIDITY")
            return
        risk_net = best.net_profit_cad
        if transfer and risk_net is not None and best.sale_proceeds_cad is not None:
            risk_net -= best.sale_proceeds_cad * strategy.transfer.haircut_pct / 100
            mark = self.market.mark(
                (await db.get(Market, UUID(buy.market_id))).exchange_id,
                (await db.get(Market, UUID(buy.market_id))).base_asset_id,
                strategy.transfer.withdrawal_fee_base + strategy.transfer.network_fee_base
                or Decimal("1e-18"),
                now,
                True,
            )
            if mark.cad_per_unit is not None:
                risk_net -= (
                    strategy.transfer.withdrawal_fee_base + strategy.transfer.network_fee_base
                ) * mark.cad_per_unit
        passes = [
            risk_net is not None and risk_net >= strategy.minimum_profit_cad,
            risk_net is not None
            and risk_net / best.acquisition_cost_cad * 100 >= strategy.minimum_profit_pct,
        ]
        if not (all(passes) if strategy.threshold_logic == "AND" else any(passes)):
            reject("PROFIT_THRESHOLD")
            return
        if (
            best.acquisition_cost_cad
            > session.initial_equity_cad * strategy.maximum_capital_per_exchange_pct / 100
        ):
            reject("VENUE_CAPITAL_LIMIT")
            return
        bm = await db.get(Market, UUID(buy.market_id))
        sm = await db.get(Market, UUID(sell.market_id))
        if not bm or not sm:
            reject("MARKET_UNAVAILABLE")
            return
        checkpoint = await db.scalar(
            select(PortfolioCheckpoint)
            .where(PortfolioCheckpoint.session_id == session.id)
            .order_by(PortfolioCheckpoint.as_of.desc())
            .limit(1)
        )
        if (
            checkpoint
            and checkpoint.payload.get("net_simulated_pnl_cad") is not None
            and (Decimal(checkpoint.payload["net_simulated_pnl_cad"]) < -strategy.maximum_loss_cad)
        ):
            reject("MAXIMUM_PORTFOLIO_LOSS")
            return
        positions = list(
            await db.scalars(
                select(PortfolioPosition).where(PortfolioPosition.session_id == session.id)
            )
        )
        exposure = Decimal(0)
        venue_reserved = Decimal(0)
        for p in positions:
            if not p.quantity:
                continue
            asset = await db.get(Asset, p.asset_id)
            mark = self.market.mark(
                p.exchange_id, p.asset_id, p.quantity, now, control.classification == "RESEARCH"
            )
            if mark.cad_per_unit is None:
                reject(
                    "INVENTORY_RISK_VALUATION_UNAVAILABLE",
                    {
                        "exchange_id": str(p.exchange_id),
                        "asset": asset.canonical_key,
                        "quantity": str(p.quantity),
                        "valuation_reason": mark.source,
                    },
                )
                return
            if asset.kind == "crypto":
                exposure += p.quantity * mark.cad_per_unit
            if p.exchange_id == bm.exchange_id:
                venue_reserved += p.reserved_quantity * mark.cad_per_unit
        if (
            venue_reserved + best.acquisition_cost_cad
            > session.initial_equity_cad * strategy.maximum_capital_per_exchange_pct / 100
        ):
            reject("VENUE_CAPITAL_LIMIT")
            return
        if (
            exposure + best.acquisition_cost_cad
            > session.initial_equity_cad * strategy.maximum_inventory_exposure_pct / 100
        ):
            reject("MAXIMUM_INVENTORY_EXPOSURE")
            return
        reserve_key = f"reserve:{trade.id}"
        requirements = [
            Reservation(
                bm.exchange_id,
                bm.quote_asset_id,
                buy_reserve,
            )
        ]
        if not transfer:
            requirements.append(Reservation(sm.exchange_id, sm.base_asset_id, sell_reserve))
        try:
            await accounting.reserve(
                db, session.id, requirements, now, reserve_key, {"trade_id": str(trade.id)}
            )
        except InsufficientBalance:
            available = {
                (p.exchange_id, p.asset_id): p.quantity - p.reserved_quantity for p in positions
            }
            reject(
                "INSUFFICIENT_PRE_FUNDED_INVENTORY",
                {
                    "requirements": [
                        {
                            "side": side,
                            "exchange_id": str(item.exchange_id),
                            "asset_id": str(item.asset_id),
                            "required": str(item.quantity),
                            "available": str(available.get((item.exchange_id, item.asset_id), 0)),
                            "deficit": str(
                                max(
                                    Decimal(0),
                                    item.quantity
                                    - available.get((item.exchange_id, item.asset_id), 0),
                                )
                            ),
                        }
                        for side, item in zip(("buy", "sell"), requirements, strict=not transfer)
                    ]
                },
            )
            return
        trade.requested_quantity = stored(best.buy_quantity)
        trade.state = {
            **trade.state,
            "reservation_key": reserve_key,
            "transfer": transfer,
            "buy_input": dump_input(buy),
            "sell_input": dump_input(sell),
            "projection": safe_json(asdict(best)),
            "legs": {},
        }
        db.add(
            PaperOrder(
                trade_id=trade.id,
                market_id=bm.id,
                side="buy",
                quantity=stored(best.buy_quantity),
                status="pending",
            )
        )
        if not transfer:
            db.add(
                PaperOrder(
                    trade_id=trade.id,
                    market_id=sm.id,
                    side="sell",
                    quantity=stored(best.sell_quantity),
                    status="pending",
                )
            )
        event(db, trade, "SCHEDULED", now, "FUNDS_RESERVED", {"due_at": due.isoformat()})
        self.metrics["scheduled"] += 1

    async def refresh_targets(self, db):
        rows = (
            await db.execute(
                select(PaperOrder, PaperTrade)
                .join(PaperTrade)
                .where(PaperTrade.status.in_(ACTIVE), PaperOrder.status == "pending")
                .limit(self.config.maximum_pending * 2)
            )
        ).all()
        targets = defaultdict(list)
        for order, trade in rows:
            strategy = trade.assumptions["strategy"]
            due = trade.eligible_at
            if order.side == "sell" and trade.state.get("transfer"):
                raw = trade.state.get("sell_eligible_at")
                if not raw:
                    continue
                from datetime import datetime

                due = datetime.fromisoformat(raw)
            targets[order.market_id].append(
                {
                    "trade_id": trade.id,
                    "order_id": order.id,
                    "opposite_id": UUID(
                        trade.state["sell_input" if order.side == "buy" else "buy_input"][
                            "market_id"
                        ]
                    ),
                    "eligible_at": due,
                    "settings": strategy,
                    "research": trade.classification == "RESEARCH",
                    "raw_due": trade.state.get("raw_due"),
                    "run_id": trade.state.get("collector_run_id"),
                }
            )
        self.targets = dict(targets)
        for mid in list(self.recent):
            if mid not in targets:
                del self.recent[mid]

    def _fx(self, payload, currency, amount):
        if currency == "CAD":
            return Decimal(1)
        value = payload.get("fx", {}).get(currency)
        if not value:
            return None
        from arbiq.opportunities.contracts import ConversionLeg, ConversionLevel, ConversionRoute

        try:
            route = ConversionRoute(
                tuple(
                    ConversionLeg(
                        x["source_currency"],
                        x["target_currency"],
                        tuple(
                            ConversionLevel(Decimal(z["price"]), Decimal(z["quantity"]))
                            for z in x["levels"]
                        ),
                        Decimal(x["fee_rate"]),
                        x["evidence"],
                    )
                    for x in value["to_cad"]["legs"]
                )
            )
            return convert(route, amount) / amount
        except (ValueError, ArithmeticError, KeyError):
            return None

    @decimal_context
    async def execute_order(self, db, trade, order, observation, now):
        strategy = read_pinned_strategy(trade.assumptions["strategy"])
        book = CapturedBook.model_validate(observation.payload["book"])
        # Stale processing never invents a fill at a past decision. The retained
        # observation is consumed at its own causally valid event time.
        clock = observation.payload["clock"]
        allowed, reason = evidence_eligibility(
            book,
            now=observation.received_at,
            eligible_at=(
                trade.eligible_at
                if order.side == "buy" or not trade.state.get("transfer")
                else __import__("datetime").datetime.fromisoformat(trade.state["sell_eligible_at"])
            ),
            maximum_age=strategy.maximum_book_age_seconds,
            research=trade.classification == "RESEARCH",
            allow_missing=strategy.allow_missing_source_timestamp,
            minimum_confidence=strategy.minimum_confidence,
            clock_trusted=clock["trusted"],
        )
        if not allowed:
            order.status = "failed"
            order.reason = reason
            return
        latest_movement = await db.scalar(
            select(func.max(AccountingMovement.effective_at)).where(
                AccountingMovement.session_id == trade.session_id,
                AccountingMovement.event_type.in_(["PAPER_LEG", "INVENTORY_TRANSFER"]),
            )
        )
        if latest_movement is not None and latest_movement > observation.received_at:
            order.status = "failed"
            order.reason = "LATE_OUT_OF_ORDER_EVIDENCE"
            return
        model = await db.get(Market, order.market_id)
        raw = trade.state["buy_input" if order.side == "buy" else "sell_input"]
        market = load_input(raw)
        reservation_key = (
            trade.state.get("sell_reservation_key", trade.state["reservation_key"])
            if order.side == "sell"
            else trade.state["reservation_key"]
        )
        if not trade.state.get("legs") and not trade.state.get("transfer"):
            other_raw = observation.payload.get("opposite_book_asof")
            other = CapturedBook.model_validate(other_raw) if other_raw else None
            try:
                recheck_projection(
                    buy_book=book if order.side == "buy" else other,
                    sell_book=book if order.side == "sell" else other,
                    buy_input=load_input(trade.state["buy_input"]),
                    sell_input=load_input(trade.state["sell_input"]),
                    planned_quantity=trade.requested_quantity,
                    decision_at=observation.received_at,
                    strategy=strategy,
                    captured_fx=observation.payload.get("fx"),
                )
            except ValueError as exc:
                order.status = "failed"
                order.reason = str(exc)
                others = list(
                    await db.scalars(
                        select(PaperOrder).where(
                            PaperOrder.trade_id == trade.id,
                            PaperOrder.id != order.id,
                            PaperOrder.status == "pending",
                        )
                    )
                )
                for other_order in others:
                    other_order.status = "cancelled"
                    other_order.reason = "PREFLIGHT_REJECTED"
                return
        owned = await accounting.owned_reservation(db, trade.session_id, reservation_key)
        asset = model.quote_asset_id if order.side == "buy" else model.base_asset_id
        available = owned.get((model.exchange_id, asset), Decimal(0))
        try:
            fill = simulate_leg(
                book,
                order.side,
                order.quantity,
                available,
                market.fee,
                market.rules,
                strategy.slippage_bps,
                strategy.maximum_slippage_bps,
            )
        except ValueError as exc:
            order.status = "failed"
            order.reason = str(exc)
            return
        quote_asset = await db.get(Asset, model.quote_asset_id)
        quote_rate = self._fx(observation.payload, quote_asset.symbol, fill.quote_notional)
        base_rate = fill.observed_vwap * quote_rate if quote_rate is not None else None
        if trade.state.get("legs"):
            changes = []
            for old in trade.state["legs"].values():
                if old.get("quote_rate_cad") is None:
                    changes = []
                    break
                changes.append(Decimal(old["quote_change"]) * Decimal(old["quote_rate_cad"]))
            proposed = -fill.quote_notional if order.side == "buy" else fill.quote_notional
            if fill.fee_currency == "quote":
                proposed -= fill.fee_quantity
            if (
                quote_rate is not None
                and changes
                and sum(changes) + proposed * quote_rate < -strategy.maximum_loss_cad
            ):
                order.status = "failed"
                order.reason = "MAXIMUM_TOLERATED_LOSS"
                return
        result = await accounting.execute_leg(
            db,
            trade.session_id,
            model.exchange_id,
            model.base_asset_id,
            model.quote_asset_id,
            order.side,
            fill.quantity,
            fill.quote_notional,
            fill.fee_quantity,
            fill.fee_currency,
            base_rate,
            quote_rate,
            observation.received_at,
            f"fill:{order.id}",
            trade.id,
            {
                "book": book.model_dump(mode="json"),
                "fill_model": safe_json(asdict(fill)),
                "clock": clock,
                "fx": observation.payload.get("fx", {}),
                "classification": trade.classification,
            },
            from_reserved=True,
            reservation_key=reservation_key,
        )
        fee_version = FeeVersion(
            exchange_id=model.exchange_id,
            market_id=model.id,
            effective_at=observation.received_at,
            maker_rate=market.fee.rate,
            taker_rate=market.fee.rate,
            source="versioned paper strategy taker",
            assumptions={
                "verified": market.fee.verified,
                "currency": market.fee.currency,
                "strategy_version_id": str(trade.strategy_version_id),
            },
        )
        db.add(fee_version)
        await db.flush()
        fx_id = None
        if quote_rate is not None:
            cad = await db.scalar(select(Asset).where(Asset.canonical_key == "fiat:CAD"))
            fx = FXQuote(
                base_asset_id=model.quote_asset_id,
                quote_asset_id=cad.id,
                rate=stored(quote_rate),
                observed_at=observation.received_at,
                source="Pinned execution observation conversion",
                provenance={
                    "fx": observation.payload.get("fx", {}),
                    "currency": quote_asset.symbol,
                },
            )
            db.add(fx)
            await db.flush()
            fx_id = fx.id
        db.add(
            PaperFill(
                order_id=order.id,
                snapshot_id=observation.snapshot_id,
                journal_id=result.journal_id,
                fee_version_id=fee_version.id,
                fx_quote_id=fx_id,
                quantity=fill.quantity,
                price=stored(fill.modeled_price),
                quote_notional=fill.quote_notional,
                fee_quantity=fill.fee_quantity,
                fee_asset_id=model.base_asset_id
                if fill.fee_currency == "base"
                else model.quote_asset_id,
                fee_cad=result.fees_cad,
                cost_basis_cad=result.cost_basis_removed_cad,
                realized_simulated_pnl_cad=result.realized_trading_cad,
                executed_at=observation.received_at,
                provenance={
                    "model": VERSION,
                    "observed_vwap": str(fill.observed_vwap),
                    "slippage_quote": str(fill.slippage_quote),
                    "classification": trade.classification,
                    "observation_id": str(observation.id),
                    "clock": clock,
                },
            )
        )
        order.status = "partial" if fill.partial else "filled"
        legs = dict(trade.state.get("legs", {}))
        legs[order.side] = {
            **safe_json(asdict(fill)),
            "base_change": str(result.base_change),
            "quote_change": str(result.quote_change),
            "quote_rate_cad": str(quote_rate) if quote_rate is not None else None,
            "fees_cad": str(result.fees_cad) if result.fees_cad is not None else None,
            "executed_at": observation.received_at.isoformat(),
            "journal_id": str(result.journal_id),
        }
        trade.state = {**trade.state, "legs": legs}
        self.metrics["filled_legs"] += 1
        db.add(
            SimulationEvent(
                trade_id=trade.id,
                order_id=order.id,
                event_type="LEG_FILLED",
                reason=order.status,
                occurred_at=observation.received_at,
                provenance={
                    "journal_id": str(result.journal_id),
                    "observation_id": str(observation.id),
                },
            )
        )

    @decimal_context
    async def transfer_after_buy(self, db, trade, now):
        strategy = read_pinned_strategy(trade.assumptions["strategy"])
        legs = trade.state.get("legs", {})
        buy = legs.get("buy")
        if not buy:
            return
        amount = Decimal(buy["base_change"])
        fee = strategy.transfer.withdrawal_fee_base + strategy.transfer.network_fee_base
        if amount <= fee:
            await self.finish(db, trade, "PARTIAL", now, "TRANSFER_FEES_EXCEED_INVENTORY")
            return
        bm = await db.get(Market, UUID(trade.state["buy_input"]["market_id"]))
        sm = await db.get(Market, UUID(trade.state["sell_input"]["market_id"]))
        if not trade.state.get("transfer_reserved"):
            key = f"transfer-reserve:{trade.id}"
            await accounting.reserve(
                db,
                trade.session_id,
                [Reservation(bm.exchange_id, bm.base_asset_id, amount)],
                now,
                key,
                {"trade_id": str(trade.id)},
            )
            from datetime import datetime

            arrived = datetime.fromisoformat(buy["executed_at"]) + timedelta(
                seconds=strategy.transfer.settlement_seconds
            )
            trade.state = {
                **trade.state,
                "transfer_reserved": key,
                "sell_eligible_at": arrived.isoformat(),
            }
            db.add(
                SimulationEvent(
                    trade_id=trade.id,
                    order_id=None,
                    event_type="TRANSFER_SCHEDULED",
                    reason="SETTLEMENT_DELAY",
                    occurred_at=now,
                    provenance={
                        "arrives_at": arrived.isoformat(),
                        "policy": strategy.transfer.model_dump(mode="json"),
                    },
                )
            )
        from datetime import datetime

        arrived = datetime.fromisoformat(trade.state["sell_eligible_at"])
        if now < arrived or trade.state.get("transfer_settled"):
            return
        rate = self.market.mark(bm.exchange_id, bm.base_asset_id, amount, now, True).cad_per_unit
        await accounting.transfer(
            db,
            trade.session_id,
            bm.exchange_id,
            sm.exchange_id,
            bm.base_asset_id,
            amount - fee,
            fee,
            rate,
            datetime.fromisoformat(buy["executed_at"]),
            now,
            f"transfer:{trade.id}",
            {
                "network": strategy.transfer.network,
                "network_compatible_verified": strategy.transfer.network_verified,
                "network_compatible_assumed": trade.classification == "RESEARCH",
                "research_only": trade.classification == "RESEARCH",
                "settlement_delay_seconds": strategy.transfer.settlement_seconds,
                "policy": strategy.transfer.model_dump(mode="json"),
            },
            reservation_key=trade.state["transfer_reserved"],
        )
        sell_input = load_input(trade.state["sell_input"])
        available = amount - fee
        quantity = available / (1 + sell_input.fee.rate if sell_input.fee.currency == "base" else 1)

        quantity = floor_derived(quantity, sell_input.rules.amount_step or Decimal("1e-18"))
        sell_reserve = f"transfer-sell-reserve:{trade.id}"
        await accounting.reserve(
            db,
            trade.session_id,
            [Reservation(sm.exchange_id, sm.base_asset_id, available)],
            now,
            sell_reserve,
            {"trade_id": str(trade.id)},
        )
        trade.state = {
            **trade.state,
            "transfer_settled": True,
            "sell_reservation_key": sell_reserve,
            "sell_eligible_at": now.isoformat(),
        }
        db.add(
            PaperOrder(
                trade_id=trade.id,
                market_id=sm.id,
                side="sell",
                quantity=stored(quantity),
                status="pending",
            )
        )
        db.add(
            SimulationEvent(
                trade_id=trade.id,
                order_id=None,
                event_type="TRANSFER_SETTLED",
                reason="FRESH_SELL_BOOK_REQUIRED",
                occurred_at=now,
                provenance={},
            )
        )

    @decimal_context
    async def finish(self, db, trade, status, now, reason):
        if trade.state.get("reservation_key"):
            await accounting.release_remaining(
                db,
                trade.session_id,
                trade.state["reservation_key"],
                now,
                f"release:{trade.id}",
                {"status": status},
            )
        if trade.state.get("sell_reservation_key"):
            await accounting.release_remaining(
                db,
                trade.session_id,
                trade.state["sell_reservation_key"],
                now,
                f"sell-release:{trade.id}",
                {"status": status},
            )
        if trade.state.get("transfer_reserved") and not trade.state.get("transfer_settled"):
            await accounting.release_remaining(
                db,
                trade.session_id,
                trade.state["transfer_reserved"],
                now,
                f"transfer-release:{trade.id}",
                {"status": status},
            )
        legs = trade.state.get("legs", {})
        result = None
        if status == "COMPLETED" and all(k in legs for k in ("buy", "sell")):
            if all(legs[k].get("quote_rate_cad") is not None for k in legs):
                net = sum(
                    (
                        Decimal(legs[k]["quote_change"]) * Decimal(legs[k]["quote_rate_cad"])
                        for k in ("buy", "sell")
                    ),
                    Decimal(0),
                )
                fees = (
                    sum((Decimal(legs[k]["fees_cad"]) for k in ("buy", "sell")), Decimal(0))
                    if all(legs[k]["fees_cad"] is not None for k in legs)
                    else None
                )
                slip = sum(
                    (
                        Decimal(legs[k]["slippage_quote"]) * Decimal(legs[k]["quote_rate_cad"])
                        for k in ("buy", "sell")
                    ),
                    Decimal(0),
                )
                result = {
                    "net_pnl_cad": str(net),
                    "gross_pnl_cad": str(net + (fees or 0) + slip),
                    "fees_cad": str(fees) if fees is not None else None,
                    "fx_expenses_cad": "0"
                    if all(
                        trade.state[k]["quote_currency"] == "CAD"
                        for k in ("buy_input", "sell_input")
                    )
                    else None,
                    "slippage_cad": str(slip),
                    "rebalancing_cad": "0",
                    "execution_cashflow_basis": (
                        "observed independent legs; fees and modeled slippage included once"
                    ),
                }
        exposure = {
            "base_change": str(sum((Decimal(v["base_change"]) for v in legs.values()), Decimal(0))),
            "unmatched": status == "PARTIAL",
            "valued_in_portfolio": True,
        }
        trade.state = {**trade.state, "result": result or {}, "exposure": exposure}
        event(db, trade, status, now, reason)
        self.metrics[status.lower()] += 1

