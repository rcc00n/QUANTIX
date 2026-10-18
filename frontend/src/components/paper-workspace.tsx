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
function Classification({ value }: { value: PaperClassification }) {
  return (
    <span className={`paper-class ${value === "RESEARCH" ? "research" : "validated"}`}>
      {value === "RESEARCH" ? "Research only" : "Validated portfolio"}
    </span>
  );
}
function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div className="paper-notice">
      <ShieldCheck size={16} />
      <span>{children}</span>
    </div>
  );
}
function Metric({
  label,
  value,
  unit = "CAD",
}: {
  label: string;
  value: string | null | undefined;
  unit?: string;
}) {
  return (
    <div>
      <span>{label}</span>
      <strong>
        <Exact value={value} unit={unit} />
      </strong>
    </div>
  );
}
const valuationLabels: Record<PortfolioValuationStatus | "ZERO_POSITION", string> = {
  COMPLETE_FRESH: "Complete fresh valuation",
  PARTIAL: "Partial valuation",
  STALE: "Stale retained valuation",
  UNKNOWN: "Valuation unavailable",
  ZERO_POSITION: "Empty position",
};

function ValuationCoverage({ portfolio }: { portfolio: PaperPortfolio }) {
  const valuation = portfolio.valuation_status;
  if (!valuation) return null;
  const unpriced = portfolio.unpriced_balances ?? [];
  return (
    <section className="paper-limitations" aria-label="Portfolio valuation coverage">
      <strong>{valuationLabels[valuation]}</strong>
      {valuation === "STALE" && <p>Retained as of {utcLabel(portfolio.as_of)}.</p>}
      {(valuation === "PARTIAL" || valuation === "STALE") &&
        portfolio.valued_subtotal_cad != null && (
          <p>
            Marked positions subtotal <Exact value={portfolio.valued_subtotal_cad} unit="CAD" />.
            Full portfolio equity requires every balance.
          </p>
        )}
      {unpriced.length > 0 && (
        <details className="planner-inventory">
          <summary>
            {portfolio.unpriced_position_count ?? unpriced.length} balances without complete marks
          </summary>
          <ul>
            {unpriced.map((balance) => (
              <li key={`${balance.exchange}:${balance.canonical_key ?? balance.asset}`}>
                {balance.exchange} · {assetLabel(balance.canonical_key ?? balance.asset)}{" "}
                <Exact value={balance.quantity} />
                {balance.reason && ` · ${balance.reason.replaceAll("_", " ")}`}
                {balance.detail_reason &&
                  balance.detail_reason !== balance.reason &&
                  ` · ${balance.detail_reason.replaceAll("_", " ")}`}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function ExecutionReasons({ metrics }: { metrics: PaperMetrics | null }) {
  const reasons = Object.entries(metrics?.failure_reasons ?? {}).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );
  if (!reasons.length) return null;
  return (
    <details className="planner-inventory paper-limitations">
      <summary>Recorded simulation outcome reasons</summary>
      <ul>
        {reasons.map(([reason, count]) => (
          <li key={reason}>
            {reason.replaceAll("_", " ")} · {count}
          </li>
        ))}
      </ul>
    </details>
  );
}
function Summary({
  session,
  portfolio,
  metrics,
}: {
  session: PaperSession;
  portfolio: PaperPortfolio | null;
  metrics: PaperMetrics | null;
}) {
  return (
    <>
      <div className="paper-summary">
        <Metric label="Initial equity" value={session.initial_equity_cad} />
        <Metric label="Current virtual equity" value={portfolio?.equity_cad} />
        <Metric
          label="Net simulated P&L"
          value={portfolio?.net_simulated_pnl_cad ?? portfolio?.net_pnl_cad}
        />
        <Metric label="Maximum drawdown" value={metrics?.maximum_drawdown_pct} unit="%" />
      </div>
      {session.classification === "RESEARCH" && (
        <Notice>
          Hypothetical research portfolio. These balances and outcomes are excluded from validated
          performance and never combined with its capital.
        </Notice>
      )}
      {portfolio && <ValuationCoverage portfolio={portfolio} />}
      {portfolio?.limitations?.length ? (
        <div className="paper-limitations">
          <strong>Valuation and evidence limitations</strong>
          <ul>
            {portfolio.limitations.map((reason) => (
              <li key={reason}>{reason.replaceAll("_", " ")}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}