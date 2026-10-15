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

const profitabilityLabel = {
  PROJECTED_POSITIVE: "Positive market projection",
  NON_POSITIVE: "Non-positive market projection",
  UNKNOWN: "Market profitability unavailable",
};
const routeReasons = (route: FundingRoute) => [
  ...new Set([...(route.reasons ?? []), ...(route.limiting_constraints ?? [])]),
];