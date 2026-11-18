export type PaperClassification = "VALIDATED" | "RESEARCH";
export const STRATEGY_LAB_PROFILES = {
  LIQUID_ARBITRAGE: "Liquid Arbitrage",
  ANOMALY_HUNTER: "Anomaly Hunter",
  HYBRID_OPTIMIZER: "Hybrid Optimizer",
  NO_TRADING: "No-trading benchmark",
} as const;
export type StrategyLabProfile = keyof typeof STRATEGY_LAB_PROFILES;
export const STRATEGY_LAB_ENTRY: Record<StrategyLabProfile, string> = {
  LIQUID_ARBITRAGE: "Healthy, timestamped books. Resolved anomalies are excluded.",
  ANOMALY_HUNTER: "A resolved anomaly requires independent fresh REST rechecks of both legs.",
  HYBRID_OPTIMIZER:
    "Healthy, timestamped books and independently confirmed anomalies are eligible.",
  NO_TRADING: "Values the original inventory without submitting orders or rebalancing.",
};
export type PaperStrategy = {
  schema_version?: string;
  preset: string;
  strategy_lab_profile?: StrategyLabProfile | null;
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
export type LabOpeningAllocation = {
  target_trade_cad: string;
  reserve_cad: string;
  unallocated_cad: string;
  allocations: {
    exchange: string;
    asset: string;
    quantity: string;
    cad_value: string;
    cad_rate: string;
  }[];
  coverage_by_size: {
    trade_amount_cad: string;
    effective_minimum_cad: string;
    considered_routes: number;
    funded_alternative_routes: number;
    simultaneous_funded_routes: number;
    profitable_funded_routes: number;
  }[];
  funded_routes: {
    route_id: string;
    base_asset: string;
    buy_exchange: string;
    sell_exchange: string;
    maximum_funded_acquisition_cad: string;
    net_profit_cad: string | null;
  }[];
};
export type PaperSession = {
  id: string;
  name: string;
  classification: PaperClassification;
  mode: string;
  status: string;
  archived_at?: string | null;
  initial_equity_cad: string;
  started_at: string;
  strategy_version_id: string;
  settings: PaperStrategy;
  lab_cohort?: {
    id: string;
    proposal_id: string;
    started_at: string;
    allocation?: LabOpeningAllocation;
  } | null;
};
export type PaperSessionScope = "current" | "archived";

export function paperSessionsPath(scope: PaperSessionScope) {
  return scope === "archived" ? "/paper/sessions?scope=archived" : "/paper/sessions";
}

export function preferredPaperSession(
  sessions: PaperSession[],
  ...preferredIds: (string | null)[]
): PaperSession | undefined {
  for (const id of preferredIds) {
    const selected = sessions.find((session) => session.id === id);
    if (selected) return selected;
  }
  const hybrid = (session: PaperSession) =>
    session.settings.strategy_lab_profile === "HYBRID_OPTIMIZER" && session.status !== "ARCHIVED";
  return (
    sessions.find((session) => hybrid(session) && session.lab_cohort) ??
    sessions.find((session) => hybrid(session) && session.status === "ACTIVE") ??
    sessions.find((session) => session.classification === "VALIDATED") ??
    sessions[0]
  );
}
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
  inventory_exposure_cad?: string | null;
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
  net_completed_trade_pnl_cad?: string | null;
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

export function assetLabel(key: string): string {
  return key.startsWith("asset:") || key.startsWith("fiat:") ? key.split(":").at(-1)! : key;
}
export function utcLabel(value: string | null | undefined): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unavailable"
    : `${date.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}
