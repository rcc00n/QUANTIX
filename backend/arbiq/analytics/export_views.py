"""Readable analytical projections; source records/evidence remain independently exported."""

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from arbiq.db.models import Asset, CollectorRun, Exchange, ExchangeHealth, LatestMarketBook, Market

OPPORTUNITY_COLUMNS = (
    "id",
    "route_id",
    "evaluated_at",
    "base_asset",
    "buy_exchange",
    "sell_exchange",
    "buy_symbol",
    "sell_symbol",
    "status",
    "actionable",
    "eligibility",
    "quantity",
    "acquisition_cost_cad",
    "net_profit_cad",
    "net_return_pct",
    "buy_vwap",
    "sell_vwap",
    "buy_snapshot_id",
    "sell_snapshot_id",
    "buy_fee_version_id",
    "sell_fee_version_id",
    "calculation_version",
    "selected_budget_cad",
    "costs",
    "budget_results",
    "maximum_executable",
    "reasons",
    "optimizer",
    "transfer_scenario",
)
PORTFOLIO_COLUMNS = (
    "id",
    "session_id",
    "as_of",
    "calculation_version",
    "initial_equity_cad",
    "equity_cad",
    "net_simulated_pnl_cad",
    "realized_simulated_pnl_cad",
    "realized_trading_pnl_cad",
    "realized_fx_pnl_cad",
    "unrealized_pnl_cad",
    "no_trading_benchmark_cad",
    "benchmark_return_pct",
    "strategy_excess_cad",
    "portfolio_return_pct",
    "drawdown_pct",
    "capital_utilization_pct",
    "fees_cad",
    "expenses_cad",
    "reconciliation_delta_cad",
    "limitations",
)


def readable_projection(table, row):
    if table == "arbitrage_evaluations":
        result = row.get("result") or {}
        costs = result.get("costs") or {}
        view = {k: row[k] if k in row else result.get(k) for k in OPPORTUNITY_COLUMNS}
        view.update(buy_vwap=costs.get("buy_vwap"), sell_vwap=costs.get("sell_vwap"))
        return "opportunities/analysis.csv", view
    if table == "portfolio_checkpoints":
        payload = row.get("payload") or {}
        return "analytics/portfolio_history.csv", {
            k: row[k] if k in row else payload.get(k) for k in PORTFOLIO_COLUMNS
        }
    return None


