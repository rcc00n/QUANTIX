"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  LoaderCircle,
  Radar,
  RefreshCw,
  X,
  ExternalLink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { decimal, utc } from "@/lib/market-data";
import { BookDepth } from "@/components/terminal-charts";
import { SizeDiagnostics } from "@/components/size-diagnostics";
import { asRecord, asRecords, text } from "@/lib/terminal";
import {
  displayDecimal,
  reasonLabel,
  type Opportunity,
  type OpportunityDiagnostics,
  type OpportunityPage,
  type SupportedBudgets,
} from "@/lib/opportunities";

const PAGE_SIZE = 25;
const emptyPage: OpportunityPage = { items: [], total: 0, limit: PAGE_SIZE, offset: 0 };

async function read<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, cache: "no-store" });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Your session expired. Sign in again.");
  }
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    throw new Error(
      typeof value?.detail === "string" ? value.detail : "Opportunity engine unavailable.",
    );
  }
  return response.json();
}

function Amount({ value, suffix = "" }: { value: string | null | undefined; suffix?: string }) {
  if (value === null || value === undefined) return <>Unavailable</>;
  const display = displayDecimal(value);
  return (
    <span title={`Exact value: ${value}${suffix}`}>
      {display.text}
      {suffix}
    </span>
  );
}

function Confidence({ value }: { value: Opportunity["status"] }) {
  return (
    <span className={`opportunity-state opportunity-${value.toLowerCase()}`}>
      {reasonLabel(value)}
    </span>
  );
}

function RecordDetails({ title, value }: { title: string; value: unknown }) {
  if (value === null || value === undefined) return null;
  return (
    <details className="evidence-details">
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}