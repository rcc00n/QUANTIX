"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Filter, ShieldCheck } from "lucide-react";
import { FullExport } from "@/components/full-export";
import { Button } from "@/components/ui/button";
import { HistoryChart, AmountBars, type SeriesPoint } from "@/components/terminal-charts";
import {
  Exact,
  Kpi,
  LiveStatus,
  SectionTitle,
  Warning,
  Empty,
  Tag,
  JsonDetails,
} from "@/components/terminal-ui";
import { asRecord, asRecords, text, useLive } from "@/lib/terminal";
import type { PaperSession, PaperPortfolio } from "@/lib/paper";
import { utcLabel } from "@/lib/paper";

type AnalyticsResult = {
  schema_version: string;
  classification: string;
  period: Record<string, unknown>;
  metrics: Record<string, unknown>;
  portfolio: PaperPortfolio | null;
  by_trade_size?: Record<string, unknown>[];
  strategy_versions?: Record<string, unknown>[];
  reconciliation?: Record<string, unknown>;
  limitations?: string[];
  expense_breakdown?: Record<string, unknown>;
};
type SeriesResult = { items: SeriesPoint[]; sampling?: unknown; available_count?: number };