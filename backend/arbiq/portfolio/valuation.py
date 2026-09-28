"""Exact portfolio marks and no-trading benchmark, with missing-price propagation."""

from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select

from arbiq.db.models import Asset, Exchange
from arbiq.market_data.quality import safe_json
from arbiq.portfolio.accounting import lock_session, reconcile
from arbiq.portfolio.contracts import ACCOUNTING_VERSION, decimal_context, stored
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint, PortfolioPosition

ZERO = Decimal(0)


def complete_sum(values):
    values = list(values)
    if any(v is None for v in values):
        return None
    with localcontext() as ctx:
        ctx.prec = 78
        return stored(sum(values, ZERO))


def valid_price(price, now, maximum_age_seconds, *, cad=False):
    if cad:
        return Decimal(1), True, None
    if price is None or price.cad_per_unit is None:
        return None, False, "CAD_PRICE_UNAVAILABLE"
    if price.observed_at is None or price.received_at is None:
        return None, False, "VALUATION_TIMESTAMP_UNAVAILABLE"
    for timestamp in (price.observed_at, price.received_at):
        age = (now - timestamp).total_seconds()
        if age < 0:
            return None, False, "VALUATION_EVIDENCE_FROM_FUTURE"
        if age > maximum_age_seconds:
            return None, False, "VALUATION_EVIDENCE_STALE"
    return price.cad_per_unit, price.verified, None


def missing_status(reason, price):
    if reason == "VALUATION_EVIDENCE_STALE":
        return "STALE"
    if price and price.cad_per_unit is None:
        blockers = price.provenance.get("valuation_blockers", []) or [price.source]
        if blockers and all(
            item in {"valuation_book_stale", "cad_conversion_stale"} for item in blockers
        ):
            return "STALE"
    return "UNKNOWN"


@decimal_context
def inventory_imbalance(positions, benchmark_holdings):
    """Units required to restore initial venue shares, excluding transfer costs."""
    initial = {}
    current = {}
    for holding in benchmark_holdings:
        asset = str(holding["asset_id"])
        venue = str(holding["exchange_id"])
        group = initial.setdefault(asset, {})
        group[venue] = group.get(venue, ZERO) + Decimal(str(holding["quantity"]))
    for position in positions:
        asset = str(position["asset_id"])
        venue = str(position["exchange_id"])
        group = current.setdefault(asset, {})
        group[venue] = group.get(venue, ZERO) + position["quantity"]
    output = []
    with localcontext() as ctx:
        ctx.prec = 78
        for asset, venues in sorted(current.items()):
            total = sum(venues.values(), ZERO)
            target = initial.get(asset, {})
            initial_total = sum(target.values(), ZERO)
            shares = (
                {venue: quantity / initial_total for venue, quantity in target.items()}
                if initial_total
                else {}
            )
            required = (
                sum(
                    (
                        abs(venues.get(venue, ZERO) - total * shares.get(venue, ZERO))
                        for venue in set(venues) | set(shares)
                    ),
                    ZERO,
                )
                / 2
                if shares
                else None
            )
            output.append(
                {
                    "asset_id": asset,
                    "current_quantity": stored(total),
                    "current_by_exchange": venues,
                    "initial_target_shares": shares,
                    "quantity_to_rebalance": stored(required),
                    "imbalance_pct": stored(required / total * 100)
                    if required is not None and total > 0
                    else None,
                    "reason": None if shares else "NO_INITIAL_ALLOCATION_TARGET",
                    "costs_included": False,
                }
            )
    return output


def calculate(
    positions,
    prices,
    benchmark_holdings,
    now,
    *,
    initial_equity_cad,
    external_flow_cad,
    realized_trading_cad,
    realized_fx_cad,
    fees_cad,
    expenses_cad,
    previous_peak_cad=None,
    benchmark_prices=None,
    maximum_age_seconds=60,
    research_only=False,
):
    """Pure calculation; positions contain IDs/symbol/quantity/reserved/basis/CAD identity.

    Fees/expenses are disclosed; realized totals already contain their cashflow or
    basis effect. They are never subtracted a second time from equity or P&L.
    """
    with localcontext() as ctx:
        ctx.prec = 78
        rows = []
        values = []
        unrealized = []
        reserved_values = []
        verified = True
        limitations = []
        unpriced_balances = []
        priced_values = []
        for p in positions:
            key = (p["exchange_id"], p["asset_id"])
            price = prices.get(key)
            rate, good, reason = valid_price(
                price, now, maximum_age_seconds, cad=p.get("canonical_key") == "fiat:CAD"
            )
            qty = p["quantity"]
            reserved = p["reserved_quantity"]
            basis = p["cost_basis_cad"]
            if qty == 0:
                value = ZERO
                unreal = ZERO
                reserved_value = ZERO
                reason = None
                status = "ZERO_POSITION"
                detail_reason = None
            else:
                value = stored(qty * rate) if rate is not None else None
                unreal = stored(value - basis) if value is not None and basis is not None else None
                reserved_value = (
                    stored(reserved * rate)
                    if rate is not None
                    else (ZERO if not reserved else None)
                )
                verified = verified and good
                detail_reason = (
                    price.source if reason and price and price.cad_per_unit is None else None
                )
                status = "COMPLETE_FRESH" if value is not None else missing_status(reason, price)
                if value is not None:
                    priced_values.append(value)
                else:
                    unpriced_balances.append(
                        {
                            **{
                                name: p.get(name)
                                for name in (
                                    "exchange",
                                    "exchange_id",
                                    "asset",
                                    "asset_id",
                                    "canonical_key",
                                    "quantity",
                                )
                            },
                            "status": status,
                            "reason": reason,
                            "detail_reason": detail_reason,
                            "blockers": price.provenance.get("valuation_blockers", [])
                            if price
                            else [],
                        }
                    )
                if reason:
                    limitations.append(reason)
                if basis is None:
                    limitations.append("COST_BASIS_UNAVAILABLE")
            values.append(value)
            unrealized.append(unreal)
            reserved_values.append(reserved_value)
            rows.append(
                {
                    **p,
                    "available_quantity": stored(qty - reserved),
                    "market_value_cad": value,
                    "unrealized_pnl_cad": unreal,
                    "cad_per_unit": rate,
                    "valuation_verified": good,
                    "valuation_reason": reason,
                    "valuation_detail_reason": detail_reason,
                    "valuation_status": status,
                    "price_evidence": asdict(price) if price else None,
                }
            )
        equity = complete_sum(values)
        if not unpriced_balances:
            valuation_status = "COMPLETE_FRESH"
        elif priced_values:
            valuation_status = "PARTIAL"
        elif all(row["status"] == "STALE" for row in unpriced_balances):
            valuation_status = "STALE"
        else:
            valuation_status = "UNKNOWN"
        unreal = complete_sum(unrealized)
        reserved_value = complete_sum(reserved_values)
        realized = complete_sum([realized_trading_cad, realized_fx_cad])
        pnl = (
            stored(equity - external_flow_cad)
            if equity is not None and external_flow_cad is not None
            else None
        )
        reconciliation = (
            stored(pnl - realized - unreal)
            if pnl is not None and realized is not None and unreal is not None
            else None
        )
        if reconciliation is not None and abs(reconciliation) > Decimal("0.000000000001"):
            limitations.append("PNL_RECONCILIATION_DIFFERENCE")
        benchmark_values = []
        for h in benchmark_holdings:
            key = (UUID(str(h["exchange_id"])), UUID(str(h["asset_id"])))
            quantity = Decimal(str(h["quantity"]))
            matching = next(
                (p for p in positions if (p["exchange_id"], p["asset_id"]) == key), None
            )
            cad = bool(matching and matching.get("canonical_key") == "fiat:CAD")
            price = (benchmark_prices if benchmark_prices is not None else prices).get(key)
            if (
                benchmark_prices is None
                and price is not None
                and price.provenance.get("valued_quantity") is not None
            ):
                if Decimal(str(price.provenance["valued_quantity"])) != quantity:
                    benchmark_values.append(None)
                    limitations.append("BENCHMARK_DEPTH_EVIDENCE_UNAVAILABLE")
                    continue
            rate, _, reason = valid_price(price, now, maximum_age_seconds, cad=cad)
            benchmark_values.append(stored(quantity * rate) if rate is not None else None)
            if reason:
                limitations.append("BENCHMARK_" + reason)
        benchmark = complete_sum(benchmark_values) if benchmark_holdings else ZERO
        peak = max(v for v in (previous_peak_cad, external_flow_cad, equity, ZERO) if v is not None)
        drawdown = (
            stored((peak - equity) / peak * 100)
            if equity is not None and peak > 0
            else (ZERO if equity == 0 else None)
        )
        utilization = (
            stored(reserved_value / equity * 100)
            if equity is not None and equity > 0 and reserved_value is not None
            else (ZERO if equity == 0 else None)
        )
        exposure = complete_sum(
            row["market_value_cad"] for row in rows if row.get("kind") != "fiat"
        )
        # Imbalance is the largest physical exchange share, not a claim that every
        # asset ought to have an equal allocation.
        by_exchange = {}
        for row in rows:
            group = by_exchange.setdefault(str(row["exchange_id"]), [])
            group.append(row["market_value_cad"])
        exchange_values = {key: complete_sum(vals) for key, vals in by_exchange.items()}
        max_share = (
            max(exchange_values.values(), default=ZERO)
            if all(v is not None for v in exchange_values.values())
            else None
        )
        return safe_json(
            {
                "schema_version": "1.0",
                "accounting_version": ACCOUNTING_VERSION,
                "as_of": now,
                "classification": "RESEARCH" if research_only else "VALIDATED",
                "research_only": research_only,
                "initial_equity_cad": initial_equity_cad,
                "funded_capital_cad": external_flow_cad,
                "unallocated_initial_cad": stored(max(ZERO, initial_equity_cad - external_flow_cad))
                if external_flow_cad is not None
                else None,
                "equity_cad": equity,
                "net_simulated_pnl_cad": pnl,
                "realized_simulated_pnl_cad": realized,
                "realized_trading_pnl_cad": realized_trading_cad,
                "realized_fx_pnl_cad": realized_fx_cad,
                "unrealized_pnl_cad": unreal,
                "fees_cad": fees_cad,
                "expenses_cad": expenses_cad,
                "portfolio_return_pct": stored(pnl / external_flow_cad * 100)
                if pnl is not None and external_flow_cad and external_flow_cad > 0
                else None,
                "no_trading_benchmark_cad": benchmark,
                "benchmark_return_pct": stored(
                    (benchmark - external_flow_cad) / external_flow_cad * 100
                )
                if benchmark is not None and external_flow_cad and external_flow_cad > 0
                else None,
                "strategy_excess_cad": stored(equity - benchmark)
                if equity is not None and benchmark is not None
                else None,
                "capital_utilization_pct": utilization,
                "reserved_capital_cad": reserved_value,
                "available_capital_cad": stored(equity - reserved_value)
                if equity is not None and reserved_value is not None
                else None,
                "available_capital_convention": (
                    "Unreserved marked equity includes assets; route-specific purchase currency "
                    "and sell inventory are still required"
                ),
                "inventory_exposure_cad": exposure,
                "inventory_exposure_pct": stored(exposure / equity * 100)
                if exposure is not None and equity and equity > 0
                else None,
                "exchange_equity_cad": exchange_values,
                "largest_exchange_share_pct": stored(max_share / equity * 100)
                if max_share is not None and equity and equity > 0
                else None,
                "high_water_equity_cad": stored(peak),
                "drawdown_pct": drawdown,
                "reconciliation_delta_cad": reconciliation,
                "valuation_complete": equity is not None,
                "valuation_status": valuation_status,
                "valued_subtotal_cad": complete_sum(priced_values)
                if priced_values or not unpriced_balances
                else None,
                "priced_position_count": len(priced_values),
                "unpriced_position_count": len(unpriced_balances),
                "unpriced_balances": unpriced_balances,
                "valuation_verified": bool(verified and equity is not None and not research_only),
                "limitations": list(dict.fromkeys(limitations)),
                "balances": rows,
                "inventory_imbalance": inventory_imbalance(positions, benchmark_holdings),
                "pnl_convention": (
                    "Equity minus contributed capital equals realized disposals plus "
                    "unrealized inventory. "
                    "Fees/expenses already affect cashflows/basis; "
                    "never subtract disclosures twice."
                ),
                "benchmark_convention": (
                    "Original funded units held unchanged, valued using contemporaneous evidence; "
                    "no future prices."
                ),
            }
        )


async def research_scope(db, session_id, requested=False):
    from arbiq.paper_trading.models import PaperControl

    control = await db.get(PaperControl, session_id)
    return control.classification == "RESEARCH" if control is not None else bool(requested)


async def snapshot(
    db,
    session_id,
    prices,
    now,
    *,
    research_only=False,
    persist=True,
    maximum_age_seconds=60,
    benchmark_prices=None,
):
    session = await lock_session(db, session_id)
    research_only = await research_scope(db, session_id, research_only)
    await reconcile(db, session_id)
    rows = (
        await db.execute(
            select(PortfolioPosition, Asset, Exchange)
            .join(Asset, Asset.id == PortfolioPosition.asset_id)
            .join(Exchange, Exchange.id == PortfolioPosition.exchange_id)
            .where(PortfolioPosition.session_id == session_id)
            .order_by(Exchange.slug, Asset.symbol)
        )
    ).all()
    positions = [
        {
            "exchange_id": p.exchange_id,
            "exchange": exchange.slug,
            "asset_id": p.asset_id,
            "asset": asset.symbol,
            "canonical_key": asset.canonical_key,
            "kind": asset.kind,
            "quantity": p.quantity,
            "reserved_quantity": p.reserved_quantity,
            "cost_basis_cad": p.cost_basis_cad,
        }
        for p, asset, exchange in rows
    ]
    movement_columns = (
        AccountingMovement.external_flow_cad,
        AccountingMovement.realized_trading_cad,
        AccountingMovement.realized_fx_cad,
        AccountingMovement.fees_cad,
        AccountingMovement.expenses_cad,
    )
    expressions = [func.count()]
    for column in movement_columns:
        expressions.extend((func.sum(column), func.count(column)))
    amounts = (
        await db.execute(select(*expressions).where(AccountingMovement.session_id == session_id))
    ).one()
    totals = [
        stored(amounts[1 + 2 * i] or ZERO) if amounts[2 + 2 * i] == amounts[0] else None
        for i in range(len(movement_columns))
    ]
    last = await db.scalar(
        select(PortfolioCheckpoint)
        .where(PortfolioCheckpoint.session_id == session_id)
        .order_by(PortfolioCheckpoint.as_of.desc(), PortfolioCheckpoint.created_at.desc())
        .limit(1)
    )
    prior = (
        Decimal(last.payload["high_water_equity_cad"])
        if last and last.payload.get("high_water_equity_cad") is not None
        else None
    )
    payload = calculate(
        positions,
        prices,
        session.benchmark_snapshot.get("holdings", []),
        now,
        initial_equity_cad=session.initial_equity_cad,
        external_flow_cad=totals[0],
        realized_trading_cad=totals[1],
        realized_fx_cad=totals[2],
        fees_cad=totals[3],
        expenses_cad=totals[4],
        previous_peak_cad=prior,
        benchmark_prices=benchmark_prices,
        maximum_age_seconds=maximum_age_seconds,
        research_only=research_only,
    )
    payload["session_id"] = str(session_id)
    payload["name"] = session.name
    if persist:
        row = PortfolioCheckpoint(
            session_id=session_id,
            as_of=now,
            payload=payload,
            calculation_version=ACCOUNTING_VERSION,
        )
        db.add(row)
        await db.flush()
        payload = {**payload, "checkpoint_id": str(row.id)}
    return payload
