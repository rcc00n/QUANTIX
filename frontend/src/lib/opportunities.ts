export type OpportunityStatus =
  "DETECTED" | "RECHECKING" | "VERIFIED_MARKET" | "RESEARCH_ONLY" | "INVALIDATED" | "EXPIRED";

/** Financial amounts are exact decimal strings supplied by the calculation engine. */
export type Opportunity = {
  id: string;
  status: OpportunityStatus;
  actionable: boolean;
  base_asset: string;
  buy_exchange: string;
  sell_exchange: string;
  buy_symbol: string;
  sell_symbol: string;
  detected_at: string;
  updated_at: string;
  expires_at: string;
  quantity: string | null;
  buy_price: string | null;
  sell_price: string | null;
  net_profit_cad: string | null;
  net_return_pct: string | null;
  acquisition_cost_cad: string | null;
  reasons: string[];
  eligibility: string;
  calculation_version: string;
  age_seconds?: number | null;
  costs?: Record<string, unknown>;
  evidence?: Record<string, unknown>;
  budget_results?: unknown;
  lifecycle?: unknown[];
  [key: string]: unknown;
};
export type OpportunityPage = {
  items: Opportunity[];
  total: number;
  limit: number;
  offset: number;
  window_limited?: boolean;
  candidate_window?: number;
  matching_retained_routes?: number;
};
export type OpportunityDiagnostics = {
  healthy?: boolean;
  clock_healthy?: boolean;
  clock?: { trusted?: boolean; state?: string; reasons?: string[]; [key: string]: unknown };
  metrics?: Record<string, number | string | null>;
  reasons?: string[];
  [key: string]: unknown;
};
export type SupportedBudgets = { budgets_cad: string[]; default_budget_cad: string };

export function reasonLabel(value: string): string {
  return value.replaceAll("_", " ").toLowerCase();
}

/** Display-only truncation. Exact calculation inputs and results stay untouched. */
export function displayDecimal(value: string, places = 8): { text: string; abbreviated: boolean } {
  if (/^[+-]?0+(?:\.0+)?[eE][+-]?\d+$/.test(value)) return { text: "0", abbreviated: false };
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) return { text: value, abbreviated: false };
  const [, sign, whole, rawFraction = ""] = match;
  const fraction = rawFraction.replace(/0+$/, "");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  if (fraction.length <= places)
    return { text: `${sign}${grouped}${fraction ? `.${fraction}` : ""}`, abbreviated: false };
  const visible = fraction.slice(0, places).replace(/0+$/, "");
  if (/^0+$/.test(whole) && !visible)
    return { text: `${sign}<0.${"0".repeat(places - 1)}1`, abbreviated: true };
  return { text: `≈${sign}${grouped}${visible ? `.${visible}` : ""}`, abbreviated: true };
}
