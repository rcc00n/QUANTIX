"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Exact, JsonDetails, Warning } from "@/components/terminal-ui";
import { terminalFetch, asRecord, text } from "@/lib/terminal";
import { assetLabel, utcLabel } from "@/lib/paper";

type Diagnostics = {
  evaluated_at: string;
  selected_maximum_buy_budget_cad: string;
  best: Record<string, unknown> | null;
  comparisons: {
    requested_buy_capital_cad: string;
    supported: boolean;
    buy_capital_cad: string | null;
    estimated_net_profit_cad: string | null;
    estimated_net_return_pct: string | null;
    required_sell_inventory: string | null;
    reasons: string[];
  }[];
  explanations: string[];
  optimizer: { optimality: string; evaluated_count: number };
  market_constraints: Record<string, unknown>;
};