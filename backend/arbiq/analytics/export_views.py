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


async def write_coverage(db, archive, snapshot_time):
    from arbiq.analytics.full_export import stream_rows

    base, quote = aliased(Asset), aliased(Asset)
    query = (
        select(
            Market.id.label("market_id"),
            Exchange.slug.label("exchange"),
            base.canonical_key.label("base_asset"),
            base.symbol.label("base_symbol"),
            quote.canonical_key.label("quote_asset"),
            quote.symbol.label("quote_symbol"),
            Market.native_symbol,
            Market.active,
            LatestMarketBook.state.label("retained_book_state"),
            LatestMarketBook.received_at.label("last_book_received_at"),
            LatestMarketBook.exchange_timestamp,
            (LatestMarketBook.run_id == ExchangeHealth.run_id).label("in_reported_collector_run"),
            CollectorRun.status.label("collector_run_status"),
            CollectorRun.heartbeat_at,
            ExchangeHealth.state.label("venue_health"),
            ExchangeHealth.latency_ms,
            func.extract("epoch", snapshot_time - LatestMarketBook.received_at).label(
                "book_age_seconds"
            ),
        )
        .join(Exchange, Exchange.id == Market.exchange_id)
        .join(base, base.id == Market.base_asset_id)
        .join(quote, quote.id == Market.quote_asset_id)
        .outerjoin(LatestMarketBook, LatestMarketBook.market_id == Market.id)
        .outerjoin(ExchangeHealth, ExchangeHealth.exchange_id == Market.exchange_id)
        .outerjoin(CollectorRun, CollectorRun.id == ExchangeHealth.run_id)
        .order_by(Market.id)
    )
    columns = [c.key for c in query.selected_columns]
    return await archive.csv(
        "exchanges/market_coverage.csv",
        columns,
        stream_rows(db, query),
        auxiliary=True,
        derived_from="markets/assets/exchanges/latest_market_books/exchange_health/collector_runs",
        scope=(
            "all retained spot listings; reported run membership and quote age "
            "are separate from actionability"
        ),
    )
