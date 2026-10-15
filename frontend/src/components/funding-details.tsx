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

const profitabilityLabel = {
  PROJECTED_POSITIVE: "Positive market projection",
  NON_POSITIVE: "Non-positive market projection",
  UNKNOWN: "Market profitability unavailable",
};
const routeReasons = (route: FundingRoute) => [
  ...new Set([...(route.reasons ?? []), ...(route.limiting_constraints ?? [])]),
];