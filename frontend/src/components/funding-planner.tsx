"use client";

import { useRef, useState, type FormEvent } from "react";
import { ArrowRight, Plus, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { terminalFetch, useLive } from "@/lib/terminal";
import { Exact, SectionTitle, Warning, Tag, Empty, JsonDetails } from "@/components/terminal-ui";
import { assetLabel, utcLabel, type PaperSession } from "@/lib/paper";
import { RebalancePreview } from "@/components/rebalance-preview";
import {
  AllocationBreakdown,
  FundingModeComparison,
  FundingRouteDetails,
  POLICY_LABELS,
  type FundingCatalog,
  type FundingCoverage,
  type FundingPolicy,
  type FundingProposal,
} from "@/components/funding-details";

const limits = [
  { key: "min_buy_capital_cad", label: "Minimum buy capital · CAD", value: "50" },
  { key: "min_cell_cad", label: "Minimum position · CAD", value: "50" },
  { key: "max_venue_pct", label: "Maximum per exchange · %", value: "60" },
  { key: "max_asset_pct", label: "Maximum per asset · %", value: "70" },
  { key: "max_reserve_pct", label: "Maximum reserve · %", value: "50" },
  { key: "max_stablecoin_pct", label: "Maximum stablecoin exposure · %", value: "50" },
] as const;

type LimitValues = Record<(typeof limits)[number]["key"], string>;
const initialLimits = Object.fromEntries(
  limits.map(({ key, value }) => [key, value]),
) as LimitValues;

type PlanAttempt = {
  name: string;
  proposal_id: string;
  capital_cad: string;
  idempotency_key: string;
  reset_from_session_id?: string;
};