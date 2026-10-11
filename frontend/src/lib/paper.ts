export type PaperClassification = "VALIDATED" | "RESEARCH";
export type PaperStrategy = {
  schema_version?: string;
  preset: string;
  starting_capital_cad: string;
  trade_amount_cad: string;
  min_buy_capital_cad?: string;
  minimum_profit_cad: string;
  minimum_profit_pct: string;
  threshold_logic: "AND" | "OR";
  reaction_delay_seconds: number;
  maximum_concurrent: number;
  mode: "PRE_FUNDED" | "TRANSFER" | "RESEARCH_SHADOW";
  research_execution_model?: "PRE_FUNDED" | "TRANSFER";
  allowed_exchanges?: string[];
  allowed_assets?: string[];
  [key: string]: unknown;
};
export type PaperSession = {
  id: string;
  name: string;
  classification: PaperClassification;
  mode: string;
  status: string;
  initial_equity_cad: string;
  started_at: string;
  strategy_version_id: string;
  settings: PaperStrategy;
};
export type PaperBalance = {
  exchange: string;
  asset: string;
  quantity: string;
  reserved_quantity: string;
  available_quantity: string;
  cost_basis_cad: string | null;
  market_value_cad: string | null;
  valuation_status?: PortfolioValuationStatus | "ZERO_POSITION";
  valuation_detail_reason?: string | null;
};
export type PortfolioValuationStatus = "COMPLETE_FRESH" | "PARTIAL" | "STALE" | "UNKNOWN";
export type PaperPortfolio = {
  equity_cad: string | null;
  net_pnl_cad?: string | null;
  net_simulated_pnl_cad?: string | null;
  realized_trading_cad?: string | null;
  realized_trading_pnl_cad?: string | null;
  realized_fx_cad?: string | null;
  realized_fx_pnl_cad?: string | null;
  unrealized_pnl_cad: string | null;
  fees_cad: string | null;
  expenses_cad: string | null;
  no_trading_benchmark_cad: string | null;
  strategy_excess_cad: string | null;
  drawdown_pct: string | null;
  capital_utilization_pct: string | null;
  portfolio_return_pct: string | null;
  balances: PaperBalance[];
  valuation_status?: PortfolioValuationStatus;
  valuation_status_at_checkpoint?: PortfolioValuationStatus | null;
  valued_subtotal_cad?: string | null;
  priced_position_count?: number;
  unpriced_position_count?: number;
  unpriced_balances?: {
    exchange: string;
    asset: string;
    canonical_key?: string;
    quantity: string;
    reason: string | null;
    detail_reason: string | null;
  }[];
  limitations: string[];
  as_of?: string;
  [key: string]: unknown;
};
export type PaperMetrics = {
  counts: Record<string, number>;
  scheduled_simulations?: number;
  completed_simulations?: number;
  partially_completed?: number;
  missed_simulations?: number;
  failed_simulations?: number;
  expired_simulations?: number;
  maximum_drawdown_pct?: string | null;
  win_rate_pct?: string | null;
  average_profit_cad?: string | null;
  failure_reasons?: Record<string, number>;
  [key: string]: unknown;
};
export type PaperTrade = {
  id: string;
  status: string;
  classification: PaperClassification;
  mode: string;
  requested_at: string;
  eligible_at: string;
  completed_at: string | null;
  reason: string | null;
  state: Record<string, unknown>;
  requested_quantity: string | null;
  session_id?: string;
  evaluation_id?: string | null;
  strategy_version_id?: string | null;
  reaction_delay_ms?: number;
  assumptions?: Record<string, unknown>;
};
export type PaperTradePage = { items: PaperTrade[]; total: number; limit: number; offset: number };
export type PaperTradeDetail = {
  trade: PaperTrade;
  orders: unknown[];
  fills: unknown[];
  events: unknown[];
  [key: string]: unknown;
};
export type StrategyOptions = {
  current: { id: string; version: number; settings: PaperStrategy } | null;
  presets: Record<string, PaperStrategy>;
  reaction_delays: number[];
};

export async function paperFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Your session expired. Sign in again.");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : Array.isArray(body?.detail)
          ? body.detail
              .map(
                (item: { loc?: string[]; msg?: string }) =>
                  `${item.loc?.slice(1).join(".") || "Setting"}: ${item.msg || "invalid value"}`,
              )
              .join(" · ")
          : "Paper service unavailable.",
    );
  return body as T;
}