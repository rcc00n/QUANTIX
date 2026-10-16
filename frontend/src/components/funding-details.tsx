"use client";

import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Exact, Tag } from "@/components/terminal-ui";
import { assetLabel, utcLabel } from "@/lib/paper";

export const POLICY_LABELS = {
  COVERAGE: "Baseline route coverage",
  LIQUIDITY_WEIGHTED: "Liquidity weighted",
  ROUTE_OPTIMIZED: "Route optimized",
  DIVERSIFIED_HYBRID: "Diversified hybrid",
  EQUAL_VENUES: "Legacy equal venues",
} as const;
export type FundingPolicy = keyof typeof POLICY_LABELS;
export type FundingCatalog = {
  exchanges: { id: string; slug: string; name: string }[];
  assets: { id: string; canonical_key: string; symbol: string; kind: string }[];
};
type Position = {
  exchange: string;
  asset?: string;
  canonical_key?: string;
  available_quantity?: string | null;
  reserved_quantity?: string | null;
};
type FundingRequirement = Position & {
  side?: "BUY" | "SELL" | "buy" | "sell";
  required_quantity?: string | null;
  missing_quantity?: string | null;
};
type ProjectedCosts = {
  fees_cad: string | null;
  fx_drag_cad: string | null;
  slippage_reserve_cad: string | null;
  rebalancing_cad: string | null;
};
export type FundingRoute = {
  route_id: string;
  asset?: string;
  buy_exchange?: string;
  sell_exchange?: string;
  inventory_sufficient?: boolean;
  funded?: boolean;
  required: FundingRequirement[];
  reasons?: string[];
  limiting_constraints?: string[];
  actionable?: boolean;
  projected_net_profit_cad?: string | null;
  maximum_funded_acquisition_cad?: string | null;
  maximum_observed_acquisition_cad?: string | null;
  funded_size_net_profit_cad?: string | null;
  projected_costs?: ProjectedCosts | null;
  market_profitability?: "PROJECTED_POSITIVE" | "NON_POSITIVE" | "UNKNOWN";
  fundable_with_reallocation?: boolean | null;
};
export type FundingCoverage = {
  session_id?: string;
  balances: Position[];
  routes: FundingRoute[];
  limitations: string[];
  window?: { limited?: boolean; limit: number };
};
type Concentration = Record<string, { cad_value: string; share_pct: string }>;
type SizeCoverage = {
  trade_amount_cad: string;
  funded_alternative_routes: number;
  simultaneous_funded_routes: number;
  profitable_funded_routes: number;
  considered_routes: number;
};
export type FundingProposal = {
  id: string;
  capital_cad: string;
  policy: FundingPolicy;
  eligibility?: "RESEARCH" | "VALIDATED";
  feasible?: boolean;
  creatable?: boolean;
  reasons?: string[];
  allocations: {
    exchange: string;
    asset: string;
    quantity: string;
    cad_rate: string;
    cad_value: string;
    evidence?: { reserve?: boolean };
  }[];
  total_cad: string;
  unallocated_cad: string;
  trade_amount_cad?: string;
  as_of?: string;
  expires_at: string;
  allocation_version?: string;
  planning_window?: { from: string; cutoff: string; historical: boolean };
  evidence?: unknown[];
  evidence_exclusions?: Record<string, number>;
  route_coverage: {
    considered_routes?: number;
    funded_route_count?: number;
    historical_window_days?: number;
    route_window_limited?: boolean;
  };
  funded_route_count?: number;
  simultaneous_funded_route_count?: number;
  median_funded_acquisition_cad?: string | null;
  allocation_summary?: {
    allocated_cad: string;
    invested_cad: string;
    reserve_cad: string;
    reserve_pct: string;
    by_exchange: Concentration;
    by_asset: Concentration;
    stablecoin_cad: string;
    stablecoin_pct: string;
    funded_venues: number;
    funded_cells: number;
  };
  coverage_by_size?: SizeCoverage[];
  requirements?: FundingRoute[];
  route_capacity?: {
    route_id: string;
    maximum_funded_acquisition_cad: string | null;
    maximum_observed_acquisition_cad: string | null;
    projected_net_profit_cad: string | null;
    target_pinned: {
      acquisition_cad: string;
      net_profit_cad: string | null;
      projected_costs?: ProjectedCosts | null;
      deficits: {
        exchange: string;
        asset: string;
        required_quantity: string;
        available_quantity: string;
        deficit_quantity: string;
      }[];
    } | null;
    limiting_constraints: string[];
    fundable_with_reallocation: boolean;
    actionable: false;
  }[];
  provenance?: unknown;
  limitations: string[];
};

function policyLabel(policy: FundingPolicy) {
  return POLICY_LABELS[policy] ?? policy.replaceAll("_", " ");
}

function count(value: number | undefined) {
  return value ?? "Unavailable";
}

export function FundingModeComparison({
  plans,
  chosen,
  setChosen,
  comparisonAsOf,
}: {
  plans: FundingProposal[];
  chosen: string | null;
  setChosen: (id: string) => void;
  comparisonAsOf?: string;
}) {
  return (
    <section className="funding-comparisons" aria-label="Equal-capital funding proposals">
      <h3>Compare allocation modes</h3>
      <p className="terminal-footnote">
        {comparisonAsOf
          ? `Shared evidence cutoff ${utcLabel(comparisonAsOf)}.`
          : "These proposals use equal settings. Check each evidence time for separately generated plans."}{" "}
        Funding counts describe hypothetical inventory coverage.
      </p>
      <div className="table-wrap" role="region" aria-label="Scrollable comparison" tabIndex={0}>
        <table className="terminal-table" aria-label="Funding mode comparison">
          <thead>
            <tr>
              <th>Mode</th>
              <th>Capital</th>
              <th>Cash reserve</th>
              <th>Alternatives funded</th>
              <th>Funded together</th>
              <th>Plan status</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((plan) => (
              <tr key={plan.id} className={plan.id === chosen ? "funding-comparison-selected" : ""}>
                <td>
                  <button
                    className="funding-select"
                    type="button"
                    aria-pressed={plan.id === chosen}
                    onClick={() => setChosen(plan.id)}
                  >
                    {policyLabel(plan.policy)}
                  </button>
                </td>
                <td>
                  <Exact value={plan.capital_cad} unit="CAD" />
                </td>
                <td>
                  <Exact value={plan.allocation_summary?.reserve_cad} unit="CAD" />
                </td>
                <td>
                  {count(plan.funded_route_count ?? plan.route_coverage.funded_route_count)} /{" "}
                  {count(plan.route_coverage.considered_routes)}
                </td>
                <td>{count(plan.simultaneous_funded_route_count)}</td>
                <td>
                  {plan.feasible === false
                    ? "Infeasible"
                    : plan.creatable === false
                      ? "Study only"
                      : plan.feasible
                        ? "Within limits"
                        : "Legacy rules"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const profitabilityLabel = {
  PROJECTED_POSITIVE: "Positive market projection",
  NON_POSITIVE: "Non-positive market projection",
  UNKNOWN: "Market profitability unavailable",
};
const routeReasons = (route: FundingRoute) => [
  ...new Set([...(route.reasons ?? []), ...(route.limiting_constraints ?? [])]),
];

function fundingLabel(route: FundingRoute, reasons: string[]) {
  if ((route.inventory_sufficient ?? route.funded) === true) return "FUNDED";
  if (!route.required?.length) return "FUNDING UNAVAILABLE";
  if (
    reasons.includes("TARGET_ACQUISITION_NOT_OBSERVED") ||
    reasons.includes("BELOW_MINIMUM_BUY_CAPITAL")
  )
    return "TARGET SIZE UNAVAILABLE";
  return "MISSING INVENTORY";
}