"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import {
  ChevronLeft,
  ChevronRight,
  CirclePause,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { displayDecimal } from "@/lib/opportunities";
import { FundingPlanner } from "@/components/funding-planner";
import { TradeInspector } from "@/components/trade-inspector";
import { BuyCapitalControl } from "@/components/buy-capital-control";
import { asRecord } from "@/lib/terminal";
import {
  assetLabel,
  paperFetch,
  utcLabel,
  type PaperClassification,
  type PaperMetrics,
  type PaperPortfolio,
  type PaperSession,
  type PaperStrategy,
  type PaperTrade,
  type PaperTradeDetail,
  type PaperTradePage,
  type PortfolioValuationStatus,
  type StrategyOptions,
} from "@/lib/paper";

function Exact({ value, unit = "" }: { value: string | null | undefined; unit?: string }) {
  if (value === null || value === undefined) return <span className="muted">Unavailable</span>;
  const display = displayDecimal(String(value));
  return (
    <span className="paper-number" title={`${value}${unit ? ` ${unit}` : ""}`}>
      {display.text}
      {unit && <small> {unit}</small>}
    </span>
  );
}
const valuationLabels: Record<PortfolioValuationStatus | "ZERO_POSITION", string> = {
  COMPLETE_FRESH: "Complete fresh valuation",
  PARTIAL: "Partial valuation",
  STALE: "Stale retained valuation",
  UNKNOWN: "Valuation unavailable",
  ZERO_POSITION: "Empty position",
};