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


