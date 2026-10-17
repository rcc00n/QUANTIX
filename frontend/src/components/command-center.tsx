"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowUpRight, ShieldCheck, Radar, Layers } from "lucide-react";
import { useLive, asRecord, asRecords, text, amount } from "@/lib/terminal";
import {
  Kpi,
  Exact,
  LiveStatus,
  Warning,
  Empty,
  Tag,
  SectionTitle,
  JsonDetails,
} from "@/components/terminal-ui";
import { HistoryChart, type SeriesPoint } from "@/components/terminal-charts";
import type { PaperSession, PaperPortfolio, PaperMetrics } from "@/lib/paper";
import { utcLabel } from "@/lib/paper";

type Overview = {
  as_of?: string;
  connected_exchanges?: number;
  venue_groups?: number;
  monitored_markets?: number;
  compatible_intersections?: number;
  scanner?: Record<string, unknown>;
  paper?: Record<string, unknown>;
  warnings?: string[];
  recent_opportunities?: Record<string, unknown>[];
  recent_simulations?: Record<string, unknown>[];
  feeds?: Record<string, unknown>[];
};