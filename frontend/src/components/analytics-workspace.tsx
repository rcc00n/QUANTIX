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
function GroupTable({ rows, title }: { rows: Record<string, unknown>[]; title: string }) {
  if (!rows.length)
    return (
      <Empty title={`No ${title.toLowerCase()} results`}>
        Groups appear when simulations have reconstructable completed CAD cashflows.
      </Empty>
    );
  const columns = Object.keys(rows[0])
    .filter((k) => typeof rows[0][k] !== "object")
    .slice(0, 7);
  return (
    <div className="table-wrap">
      <table className="terminal-table" aria-label={title}>
        <thead>
          <tr>
            {columns.map((k) => (
              <th key={k}>{k.replaceAll("_", " ")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 30).map((row, i) => (
            <tr key={i}>
              {columns.map((k) => (
                <td key={k}>
                  {typeof row[k] === "string" &&
                  /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(String(row[k])) ? (
                    <Exact value={row[k]} />
                  ) : (
                    text(row[k])
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}