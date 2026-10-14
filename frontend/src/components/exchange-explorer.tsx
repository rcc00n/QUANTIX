"use client";
import { MarketAge } from "@/components/market-age";
import { useLive } from "@/lib/terminal";
import { useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownUp,
  Check,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldQuestion,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  age,
  decimal,
  utc,
  type Book,
  type DataState,
  type DiscoveredMarket,
  type ExchangeFeed,
  type MarketPage,
  type MarketTier,
  type NativeStreamStatus,
} from "@/lib/market-data";

const marketTierLabels: Record<MarketTier, string> = {
  FAST_LANE: "Fast lane",
  STANDARD: "Standard",
  RESEARCH: "Research",
};

function State({ state }: { state: DataState }) {
  return (
    <span className={`data-state state-${state.toLowerCase()}`}>
      <i />
      {state}
    </span>
  );
}