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

function Detail({
  opportunity,
  recheck,
  busy,
  message,
  budget,
}: {
  opportunity: Opportunity;
  recheck: () => void;
  busy: boolean;
  message: string;
  budget: string;
}) {
  const evidence = asRecord(opportunity.evidence);
  const timing = asRecord(evidence.timing);
  const links = asRecord(opportunity.official_market_links);
  const safeLink = (value: unknown) => {
    if (typeof value !== "string") return null;
    try {
      const url = new URL(value);
      return url.protocol === "https:" ? url.href : null;
    } catch {
      return null;
    }
  };
  return (
    <section className="opportunity-inspector" aria-label="Opportunity detail">
      <div className="book-heading">
        <div>
          <span className="small-label">CALCULATION EVIDENCE</span>
          <h3>
            {opportunity.base_asset} · {opportunity.buy_exchange} → {opportunity.sell_exchange}
          </h3>
          <p>Pre-funded scenario · projected values · no orders or simulated fills</p>
          <p className="inspector-expiry-note">
            Opening this evidence does not preserve its prices or expected profitability.
          </p>
        </div>
        <Button variant="outline" size="sm" disabled={busy} onClick={recheck}>
          <RefreshCw size={13} className={busy ? "spin" : ""} /> Recheck books
        </Button>
      </div>
      {message && (
        <p role="status" className="catalog-meta">
          {message}
        </p>
      )}
      <div className="radar-detail-status">
        <Confidence value={opportunity.status} />
        <span>Book quality: {text(opportunity.data_confidence)}</span>
        <span>Trading eligibility: {reasonLabel(opportunity.eligibility)}</span>
        <span>{opportunity.actionable ? "Eligibility checks passed" : "Not actionable"}</span>
      </div>
      <dl className="book-metadata radar-metadata">
        <div>
          <dt>Detected · UTC</dt>
          <dd>{utc(opportunity.detected_at)}</dd>
        </div>
        <div>
          <dt>Updated · UTC</dt>
          <dd>{utc(opportunity.updated_at)}</dd>
        </div>
        <div>
          <dt>Expires · UTC</dt>
          <dd>{utc(opportunity.expires_at)}</dd>
        </div>
        <div>
          <dt>Calculation version</dt>
          <dd>{opportunity.calculation_version}</dd>
        </div>
      </dl>
      <p className="catalog-meta">
        Freshness at the pinned evaluation time; retained ages do not represent a new live quote.
      </p>
      <dl className="book-metadata radar-metadata">
        {[
          ["Buy book age · s", timing.buy_book_age_seconds],
          ["Sell book age · s", timing.sell_book_age_seconds],
          ["Buy source age · s", timing.buy_source_age_seconds],
          ["Sell source age · s", timing.sell_source_age_seconds],
          ["Cross-book source difference · s", timing.cross_book_source_skew_seconds],
          ["Receipt difference · s", timing.cross_book_receive_skew_seconds],
          ["Capture uncertainty · s", timing.cross_book_uncertainty_seconds],
          ["Oldest FX graph evidence · s", timing.fx_evidence_oldest_age_seconds],
          ["Route evaluation · ms", timing.route_evaluation_ms],
        ].map(([label, value]) => (
          <div key={String(label)}>
            <dt>{String(label)}</dt>
            <dd>{text(value, "Unavailable")}</dd>
          </div>
        ))}
        <div>
          <dt>Clock confidence</dt>
          <dd>
            {timing.clock_trusted === true ? "Trusted UTC" : "Unverified"}; exchange clock accuracy
            independently unverified
          </dd>
        </div>
      </dl>
      {!!opportunity.reasons.length && (
        <div className="quality-reasons">
          {opportunity.reasons.map((reason) => (
            <span key={reason}>
              <AlertTriangle size={11} />
              {reasonLabel(reason)}
            </span>
          ))}
        </div>
      )}
      <div className="radar-costs">
        <div>
          <h4>Executable proposal</h4>
          <dl className="settings-list">
            <div>
              <dt>Base quantity</dt>
              <dd>
                <Amount value={opportunity.quantity} />
              </dd>
            </div>
            <div>
              <dt>Weighted buy price · {opportunity.buy_symbol}</dt>
              <dd>
                <Amount value={opportunity.buy_price} />
              </dd>
            </div>
            <div>
              <dt>Weighted sell price · {opportunity.sell_symbol}</dt>
              <dd>
                <Amount value={opportunity.sell_price} />
              </dd>
            </div>
            <div>
              <dt>Acquisition capital · CAD</dt>
              <dd>
                <Amount value={opportunity.acquisition_cost_cad} />
              </dd>
            </div>
            <div>
              <dt>Projected net P&amp;L · CAD</dt>
              <dd>
                <Amount value={opportunity.net_profit_cad} />
              </dd>
            </div>
            <div>
              <dt>Net return on acquisition cost</dt>
              <dd>
                <Amount value={opportunity.net_return_pct} suffix="%" />
              </dd>
            </div>
          </dl>
        </div>
        <div>
          <h4>Cost breakdown</h4>
          {opportunity.costs && Object.keys(opportunity.costs).length ? (
            <dl className="settings-list">
              {Object.entries(opportunity.costs).map(([key, value]) => (
                <div key={key}>
                  <dt>{reasonLabel(key)}</dt>
                  <dd>
                    {value === null ? (
                      "Unavailable"
                    ) : typeof value === "object" ? (
                      <code>{JSON.stringify(value)}</code>
                    ) : typeof value === "string" &&
                      /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(value) ? (
                      <Amount value={value} />
                    ) : (
                      String(value)
                    )}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="catalog-meta">
              No executable cost calculation is available for this evidence.
            </p>
          )}
        </div>
      </div>
      <p className="feed-disclosure">
        Purchase currency must already be available at the buy venue and base inventory at the sell
        venue. Funding is an unverified assumption; projected profit is not realized profit.
      </p>
      <div className="radar-inventory-requirements">
        <h4>Required pre-funded inventory</h4>
        <dl className="settings-list">
          {Object.entries(asRecord(opportunity.required_inventory)).flatMap(([venue, units]) =>
            Object.entries(asRecord(units)).map(([asset, quantity]) => (
              <div key={`${venue}:${asset}`}>
                <dt>
                  {venue} · {asset}
                </dt>
                <dd>
                  <Amount value={typeof quantity === "string" ? quantity : null} />
                </dd>
              </div>
            )),
          )}
          <div>
            <dt>Depth-constrained maximum acquisition · CAD</dt>
            <dd>
              <Amount
                value={
                  typeof opportunity.maximum_liquidity_cad === "string"
                    ? opportunity.maximum_liquidity_cad
                    : null
                }
              />
            </dd>
          </div>
        </dl>
      </div>
      <div className="radar-depth-grid">
        {(
          [
            ["buy", "BUY venue ask depth"],
            ["sell", "SELL venue bid depth"],
          ] as const
        ).map(([side, label]) => {
          const book = asRecord(evidence[`${side}_book`]);
          const normalized = asRecord(book.normalized);
          const bids = asRecords(normalized.bids).map((x) => ({
            price: text(x.price, "0"),
            quantity: text(x.quantity, "0"),
          }));
          const asks = asRecords(normalized.asks).map((x) => ({
            price: text(x.price, "0"),
            quantity: text(x.quantity, "0"),
          }));
          const link = safeLink(links[side]);
          return (
            <section key={side}>
              <h4>{label}</h4>
              <BookDepth label={label} bids={bids} asks={asks} />
              <p className="terminal-footnote">
                Per-level observed units, not guaranteed fill.{" "}
                {link ? (
                  <a href={link} target="_blank" rel="noopener noreferrer" className="text-link">
                    Official trading screen <ExternalLink size={12} />
                  </a>
                ) : (
                  "Verified market URL unavailable."
                )}
              </p>
            </section>
          );
        })}
      </div>
      <SizeDiagnostics routeId={opportunity.id} budget={budget} />
      <RecordDetails
        title="Required inventory, limits and market metadata"
        value={{
          required_inventory: opportunity.required_inventory || evidence.inventory_requirements,
          capital: opportunity.required_capital || opportunity.capital_requirements,
          markets: asRecord(evidence.calculation_inputs).buy,
          liquidity: opportunity.maximum_quantity || opportunity.liquidity_constraints,
        }}
      />
      <RecordDetails
        title="Order books, fees, FX and eligibility provenance"
        value={opportunity.evidence}
      />
      <RecordDetails title="Other capital budgets" value={opportunity.budget_results} />
      <RecordDetails title="Opportunity lifecycle" value={opportunity.lifecycle} />
    </section>
  );
}