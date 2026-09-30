"""Stable numerical analytics over accounting and simulation records, never AI text."""

from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import Numeric, String, case, cast, func, select

from arbiq.db.models import MarketQualityEvent, PaperFill, PaperOrder, PaperTrade, SimulationEvent
from arbiq.market_data.quality import safe_json
from arbiq.paper_trading.lifecycle import lifecycle_rank
from arbiq.portfolio.accounting import lock_session
from arbiq.portfolio.contracts import ACCOUNTING_VERSION, stored
from arbiq.portfolio.models import AccountingMovement, DailyAccountingSnapshot, PortfolioCheckpoint
from arbiq.portfolio.valuation import complete_sum, research_scope, snapshot

REPORT_TIMEZONE = "America/Edmonton"
ZERO = Decimal(0)


def _amount(value):
    if value is None:
        return None
    if not isinstance(value, (str, Decimal)):
        raise ValueError("Analytics amounts must be exact decimal strings")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Nonfinite analytical amount")
    return result


def summarize_trades(trades, *, research_only=False):
    counts = Counter()
    reasons = Counter()
    by_route = defaultdict(list)
    by_asset = defaultdict(list)
    by_delay = defaultdict(list)
    by_strategy = defaultdict(list)
    completed_pnl = []
    gross = []
    fees = []
    fx = []
    slippage = []
    rebalance = []
    desired = "RESEARCH" if research_only else "VALIDATED"
    for trade in trades:
        if trade.get("classification", "VALIDATED") != desired:
            continue
        status = trade["status"].upper()
        counts[status] += 1
        if trade.get("reason") and status in {"MISSED", "FAILED", "EXPIRED", "PARTIAL"}:
            reasons[trade["reason"]] += 1
        result = trade.get("state", {}).get("result", {})
        if status != "COMPLETED":
            continue
        pnl = _amount(result.get("net_pnl_cad"))
        if pnl is None:
            continue
        completed_pnl.append(pnl)
        for dest, key in (
            (gross, "gross_pnl_cad"),
            (fees, "fees_cad"),
            (fx, "fx_expenses_cad"),
            (slippage, "slippage_cad"),
            (rebalance, "rebalancing_cad"),
        ):
            dest.append(_amount(result.get(key)))
        assumptions = trade.get("assumptions", {})
        route = (
            assumptions.get("buy_exchange", "unknown")
            + "→"
            + assumptions.get("sell_exchange", "unknown")
        )
        by_route[route].append(pnl)
        by_asset[assumptions.get("base_asset") or assumptions.get("asset", "unknown")].append(pnl)
        by_delay[str(trade.get("reaction_delay_ms", 0))].append(pnl)
        by_strategy[str(trade.get("strategy_version_id") or "legacy")].append(pnl)
    with localcontext() as ctx:
        ctx.prec = 78
        net = complete_sum(completed_pnl) if completed_pnl else None

        def group(values):
            return [
                {
                    "key": key,
                    "completed_with_cad_pnl": len(pnls),
                    "net_simulated_pnl_cad": stored(sum(pnls, ZERO)),
                }
                for key, pnls in sorted(values.items())
            ]

        routes = group(by_route)
        ranked = sorted(routes, key=lambda x: x["net_simulated_pnl_cad"])
        return safe_json(
            {
                "schema_version": "1.0",
                "classification": desired,
                "counts": dict(counts),
                "total_candidates": sum(counts.values()),
                "completed_simulations": counts["COMPLETED"],
                "partially_completed": counts["PARTIAL"],
                "missed_simulations": counts["MISSED"],
                "failed_simulations": counts["FAILED"],
                "expired_simulations": counts["EXPIRED"],
                "scheduled_simulations": sum(
                    counts[s] for s in ("SCHEDULED", "RECHECKING", "EXECUTING")
                ),
                "completed_with_available_cad_pnl": len(completed_pnl),
                "win_rate_pct": stored(
                    Decimal(sum(x > 0 for x in completed_pnl)) / len(completed_pnl) * 100
                )
                if completed_pnl
                else None,
                "average_completed_trade_profit_cad": stored(net / len(completed_pnl))
                if completed_pnl
                else None,
                "net_completed_trade_pnl_cad": net,
                "gross_completed_trade_pnl_cad": complete_sum(gross) if completed_pnl else None,
                "completed_trade_fees_cad": complete_sum(fees) if completed_pnl else None,
                "completed_trade_fx_expenses_cad": complete_sum(fx) if completed_pnl else None,
                "completed_trade_slippage_cad": complete_sum(slippage) if completed_pnl else None,
                "completed_trade_rebalancing_cad": complete_sum(rebalance)
                if completed_pnl
                else None,
                "best_route": ranked[-1] if ranked else None,
                "worst_route": ranked[0] if ranked else None,
                "by_route": routes,
                "by_asset": group(by_asset),
                "by_reaction_delay_ms": group(by_delay),
                "by_strategy": group(by_strategy),
                "failure_reasons": dict(reasons),
                "statistical_definition": (
                    "Only COMPLETED simulations with available CAD cashflow P&L enter win rate. "
                    "Missing samples remain null, not invented zeros."
                ),
                "research_isolation": (
                    "RESEARCH is excluded from VALIDATED statistics; "
                    "sessions and ledger balances never mix."
                ),
            }
        )


async def performance(
    db,
    session_id,
    *,
    research_only=False,
    period_start=None,
    period_end=None,
    strategy_version_id=None,
):
    """SQL aggregates preserve complete totals without loading unbounded history.

    Group response windows are explicit. Financial totals never use a row LIMIT.
    Requested-cohort counts and completion-date results are separately labeled.
    """
    research_only = await research_scope(db, session_id, research_only)
    classification = "RESEARCH" if research_only else "VALIDATED"
    base = [PaperTrade.session_id == session_id, PaperTrade.classification == classification]
    if strategy_version_id is not None:
        base.append(PaperTrade.strategy_version_id == strategy_version_id)
    cohort = list(base)
    if period_start is not None:
        cohort.append(PaperTrade.requested_at >= period_start)
    if period_end is not None:
        cohort.append(PaperTrade.requested_at < period_end)
    status_at_cutoff = func.upper(PaperTrade.status)
    if period_end is not None:
        # A terminal result recorded after a historical cutoff was still pending.
        # Earlier lifecycle events are immutable; never use today's status as history.
        prior_status = (
            select(SimulationEvent.event_type)
            .where(
                SimulationEvent.trade_id == PaperTrade.id,
                SimulationEvent.occurred_at < period_end,
                SimulationEvent.event_type.in_(
                    ("CANDIDATE", "ELIGIBILITY_CHECK", "SCHEDULED", "RECHECKING", "EXECUTING")
                ),
            )
            .order_by(
                SimulationEvent.occurred_at.desc(),
                lifecycle_rank(SimulationEvent.event_type).desc(),
                SimulationEvent.id.desc(),
            )
            .limit(1)
            .correlate(PaperTrade)
            .scalar_subquery()
        )
        status_at_cutoff = case(
            (PaperTrade.completed_at < period_end, func.upper(PaperTrade.status)),
            else_=func.coalesce(prior_status, "CANDIDATE"),
        )
    states = (
        await db.execute(
            select(status_at_cutoff, func.count()).where(*cohort).group_by(status_at_cutoff)
        )
    ).all()
    counts = Counter(dict(states))
    reasons = dict(
        (
            await db.execute(
                select(PaperTrade.reason, func.count())
                .where(
                    *cohort,
                    PaperTrade.reason.is_not(None),
                    func.upper(PaperTrade.status).in_(("MISSED", "FAILED", "EXPIRED", "PARTIAL")),
                    *([PaperTrade.completed_at < period_end] if period_end else []),
                )
                .group_by(PaperTrade.reason)
                .limit(1000)
            )
        ).all()
    )
    # Completion-date results do not disappear into a previous day's requested cohort.
    completed = [*base, func.upper(PaperTrade.status) == "COMPLETED"]
    if period_start is not None:
        completed.append(PaperTrade.completed_at >= period_start)
    if period_end is not None:
        completed.append(PaperTrade.completed_at < period_end)
    result = PaperTrade.state["result"]
    money = {
        name: cast(result[name].as_string(), Numeric(78, 36))
        for name in (
            "net_pnl_cad",
            "gross_pnl_cad",
            "fees_cad",
            "fx_expenses_cad",
            "slippage_cad",
            "rebalancing_cad",
        )
    }
    known = [*completed, money["net_pnl_cad"].is_not(None)]
    expressions = [
        func.count(),
        func.coalesce(func.sum(case((money["net_pnl_cad"] > 0, 1), else_=0)), 0),
    ]
    for column in money.values():
        expressions.extend((func.sum(column), func.count(column)))
    row = (await db.execute(select(*expressions).where(*known))).one()
    size, wins = row[:2]
    amounts = {
        name: stored(row[2 + 2 * i]) if row[3 + 2 * i] == size and size else None
        for i, name in enumerate(money)
    }

    async def grouped(expression):
        rows = (
            await db.execute(
                select(expression.label("key"), func.count(), func.sum(money["net_pnl_cad"]))
                .where(*known)
                .group_by(expression)
                .order_by(expression)
                .limit(1001)
            )
        ).all()
        return (
            [
                {
                    "key": str(key),
                    "completed_with_cad_pnl": number,
                    "net_simulated_pnl_cad": stored(total),
                }
                for key, number, total in rows[:1000]
            ],
            len(rows) > 1000,
        )

    route = (
        func.coalesce(PaperTrade.assumptions["buy_exchange"].as_string(), "unknown")
        + "→"
        + func.coalesce(PaperTrade.assumptions["sell_exchange"].as_string(), "unknown")
    )
    routes, route_limited = await grouped(route)
    assets, asset_limited = await grouped(
        func.coalesce(
            PaperTrade.assumptions["base_asset"].as_string(),
            PaperTrade.assumptions["asset"].as_string(),
            "unknown",
        )
    )
    delays, delay_limited = await grouped(cast(PaperTrade.reaction_delay_ms, String))
    strategies, strategy_limited = await grouped(
        func.coalesce(cast(PaperTrade.strategy_version_id, String), "legacy")
    )
    sizes, size_limited = await grouped(
        func.coalesce(PaperTrade.assumptions["strategy"]["trade_amount_cad"].as_string(), "unknown")
    )
    route_total = func.sum(money["net_pnl_cad"])

    async def ranked(descending):
        query = select(route, func.count(), route_total).where(*known).group_by(route)
        value = (
            await db.execute(
                query.order_by(route_total.desc() if descending else route_total.asc()).limit(1)
            )
        ).first()
        return (
            {
                "key": value[0],
                "completed_with_cad_pnl": value[1],
                "net_simulated_pnl_cad": stored(value[2]),
            }
            if value
            else None
        )

    latest = await db.scalar(
        select(PortfolioCheckpoint)
        .where(
            PortfolioCheckpoint.session_id == session_id,
            *([PortfolioCheckpoint.as_of < period_end] if period_end else []),
        )
        .order_by(PortfolioCheckpoint.as_of.desc(), PortfolioCheckpoint.created_at.desc())
        .limit(1)
    )
    movement_where = [AccountingMovement.session_id == session_id]
    if period_start is not None:
        movement_where.append(AccountingMovement.effective_at >= period_start)
    if period_end is not None:
        movement_where.append(AccountingMovement.effective_at < period_end)
    columns = {
        "realized_trading_pnl_cad": AccountingMovement.realized_trading_cad,
        "realized_fx_pnl_cad": AccountingMovement.realized_fx_cad,
        "fees_cad": AccountingMovement.fees_cad,
        "expenses_cad": AccountingMovement.expenses_cad,
        "external_flows_cad": AccountingMovement.external_flow_cad,
    }
    expression = [func.count()]
    for column in columns.values():
        expression.extend((func.sum(column), func.count(column)))
    accounting_row = (await db.execute(select(*expression).where(*movement_where))).one()
    accounting = {
        name: stored(accounting_row[1 + 2 * i] or ZERO)
        if accounting_row[2 + 2 * i] == accounting_row[0]
        else None
        for i, name in enumerate(columns)
    }
    accounting["movement_count"] = accounting_row[0]
    checkpoint_where = [PortfolioCheckpoint.session_id == session_id]
    if period_start is not None:
        checkpoint_where.append(PortfolioCheckpoint.as_of >= period_start)
    if period_end is not None:
        checkpoint_where.append(PortfolioCheckpoint.as_of < period_end)
    maximum, mean_reserved = (
        await db.execute(
            select(
                func.max(
                    cast(PortfolioCheckpoint.payload["drawdown_pct"].as_string(), Numeric(38, 18))
                ),
                func.avg(
                    cast(
                        PortfolioCheckpoint.payload["reserved_capital_cad"].as_string(),
                        Numeric(38, 18),
                    )
                ),
            ).where(*checkpoint_where)
        )
    ).one()
    ever_scheduled = await db.scalar(
        select(func.count(func.distinct(SimulationEvent.trade_id)))
        .join(PaperTrade, PaperTrade.id == SimulationEvent.trade_id)
        .where(
            *cohort,
            SimulationEvent.event_type == "SCHEDULED",
            *([SimulationEvent.occurred_at < period_end] if period_end else []),
        )
    )
    executed = await db.scalar(
        select(func.count(func.distinct(PaperTrade.id)))
        .join(PaperOrder)
        .join(PaperFill)
        .where(*cohort, *([PaperFill.executed_at < period_end] if period_end else []))
    )
    with localcontext() as ctx:
        ctx.prec = 78
        return safe_json(
            {
                "schema_version": "1.0",
                "session_id": str(session_id),
                "classification": classification,
                "counts": dict(counts),
                "total_candidates": sum(counts.values()),
                "eligible_opportunities": int(ever_scheduled),
                "ineligible_opportunities": sum(counts.values()) - int(ever_scheduled),
                "scheduled_ever": int(ever_scheduled),
                "executed_simulations": int(executed),
                "research_only_opportunities": sum(counts.values()) if research_only else 0,
                "completed_simulations": counts["COMPLETED"],
                "completed_in_period": await db.scalar(
                    select(func.count()).select_from(PaperTrade).where(*completed)
                ),
                "partially_completed": counts["PARTIAL"],
                "missed_simulations": counts["MISSED"],
                "failed_simulations": counts["FAILED"],
                "expired_simulations": counts["EXPIRED"],
                "scheduled_simulations": sum(
                    counts[s] for s in ("SCHEDULED", "RECHECKING", "EXECUTING")
                ),
                "completed_with_available_cad_pnl": size,
                "win_rate_pct": stored(Decimal(wins) / size * 100) if size else None,
                "average_completed_trade_profit_cad": stored(amounts["net_pnl_cad"] / size)
                if size
                else None,
                "net_completed_trade_pnl_cad": amounts["net_pnl_cad"],
                "gross_completed_trade_pnl_cad": amounts["gross_pnl_cad"],
                "completed_trade_fees_cad": amounts["fees_cad"],
                "completed_trade_fx_expenses_cad": amounts["fx_expenses_cad"],
                "completed_trade_slippage_cad": amounts["slippage_cad"],
                "completed_trade_rebalancing_cad": amounts["rebalancing_cad"],
                "by_route": routes,
                "by_asset": assets,
                "by_reaction_delay_ms": delays,
                "by_strategy": strategies,
                "by_trade_size": sizes,
                "best_route": await ranked(True),
                "worst_route": await ranked(False),
                "failure_reasons": reasons,
                "group_window": 1000,
                "group_window_limited": any(
                    (route_limited, asset_limited, delay_limited, strategy_limited, size_limited)
                ),
                "maximum_drawdown_pct": stored(maximum),
                "mean_reserved_capital_cad": stored(mean_reserved),
                "pnl_per_mean_reserved_capital_pct": stored(
                    amounts["net_pnl_cad"] / mean_reserved * 100
                )
                if amounts["net_pnl_cad"] is not None and mean_reserved and mean_reserved > 0
                else None,
                "portfolio": latest.payload if latest else None,
                "accounting": accounting,
                "period_definition": (
                    "Activity counts use requested_at cohorts; cashflow results use completed_at "
                    "and accounting uses effective_at. Half-open UTC periods."
                ),
                "statistical_definition": (
                    "Only COMPLETED simulations with available CAD cashflow P&L enter win rate; "
                    "unavailable samples remain null."
                ),
                "research_isolation": (
                    "RESEARCH and VALIDATED are separate sessions and filtered classifications. "
                    "Totals never silently truncate history."
                ),
            }
        )


def day_bounds(now):
    local = now.astimezone(ZoneInfo(REPORT_TIMEZONE))
    start = datetime.combine(local.date(), time.min, tzinfo=ZoneInfo(REPORT_TIMEZONE))
    end = datetime.combine(
        local.date() + timedelta(days=1), time.min, tzinfo=ZoneInfo(REPORT_TIMEZONE)
    )
    return local.date().isoformat(), start.astimezone(UTC), end.astimezone(UTC)


