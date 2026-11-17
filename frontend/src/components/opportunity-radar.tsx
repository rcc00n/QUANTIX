"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
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

function RouteSummary({ route }: { route: Opportunity }) {
  const data = route.classifications;
  if (!data) return null;
  const size = data.depth_executable_positive;
  return (
    <small className="eligibility-note">
      {size === true
        ? "Positive at recorded size"
        : size === false
          ? "No positive depth result"
          : "Executable profitability unknown"}
      {" · "}
      {data.inventory_funded === null
        ? "Inventory unassessed"
        : data.inventory_funded
          ? "Inventory funded"
          : "Inventory insufficient"}
    </small>
  );
}

function RouteEvidence({ route }: { route: Opportunity }) {
  const data = route.classifications;
  if (!data) return null;
  const freshness = {
    CURRENT: "Current received books",
    RETAINED: "Retained evaluation",
    UNVERIFIED: "Current freshness unverified",
    STALE: "Stale or invalid timing",
  }[data.freshness];
  return (
    <div className="radar-costs">
      <div>
        <h4>Route evidence</h4>
        <dl className="settings-list">
          <div>
            <dt>Book freshness</dt>
            <dd>{freshness}</dd>
          </div>
          {(
            [
              ["Raw book discrepancy", data.raw_price_discrepancy],
              ["Positive before fees and reserves", data.theoretically_positive],
              ["Modeled depth supports the selected size", data.depth_feasible],
              ["Depth-executable positive CAD", data.depth_executable_positive],
              ["Recorded receipt times within skew limit", data.receipts_within_skew_limit],
              ["Recorded source times within skew limit", data.sources_within_skew_limit],
            ] as const
          ).map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value === null ? "Unknown" : value ? "Yes" : "No"}</dd>
            </div>
          ))}
          <div>
            <dt>Depth-constrained maximum buy capital · CAD</dt>
            <dd>
              <Amount value={route.maximum_liquidity_cad} />
            </dd>
          </div>
        </dl>
      </div>
      <div>
        <h4>Funding and account evidence</h4>
        <dl className="settings-list">
          <div>
            <dt>Both sides funded</dt>
            <dd>
              {data.inventory_funded === null ? (
                <>
                  Unassessed · <Link href="/portfolio">Funding Planner</Link>
                </>
              ) : data.inventory_funded ? (
                "Yes"
              ) : (
                "No"
              )}
            </dd>
          </div>
          <div>
            <dt>Account fees verified</dt>
            <dd>{data.fee_verified === null ? "Unknown" : data.fee_verified ? "Yes" : "No"}</dd>
          </div>
          <div>
            <dt>Buy / sell fee evidence</dt>
            <dd>
              {data.fee_evidence.buy ? reasonLabel(data.fee_evidence.buy) : "Unknown"} /{" "}
              {data.fee_evidence.sell ? reasonLabel(data.fee_evidence.sell) : "Unknown"}
            </dd>
          </div>
          <div>
            <dt>Account eligibility</dt>
            <dd>
              {data.account_eligible === null
                ? "Unverified"
                : data.account_eligible
                  ? "Eligible"
                  : "Ineligible"}
            </dd>
          </div>
          <div>
            <dt>Actionable</dt>
            <dd>{data.actionable ? "Yes" : "No"}</dd>
          </div>
        </dl>
      </div>
    </div>
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
      <RouteEvidence route={opportunity} />
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

export function OpportunityRadar() {
  const [page, setPage] = useState<OpportunityPage>(emptyPage);
  const [diagnostics, setDiagnostics] = useState<OpportunityDiagnostics | null>(null);
  const [budgets, setBudgets] = useState<string[]>([]);
  const [budget, setBudget] = useState("1000");
  const [customBudget, setCustomBudget] = useState("");
  const [asset, setAsset] = useState("");
  const [assetQuery, setAssetQuery] = useState("");
  const [exchange, setExchange] = useState("");
  const [exchanges, setExchanges] = useState<{ slug: string; name: string }[]>([]);
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState("net_profit");
  const [history, setHistory] = useState(false);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Opportunity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [rechecking, setRechecking] = useState(false);
  const [recheckMessage, setRecheckMessage] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [search, setSearch] = useState("");
  const [minimumProfit, setMinimumProfit] = useState("");
  const [minimumReturn, setMinimumReturn] = useState("");
  const [minimumQuantity, setMinimumQuantity] = useState("");
  const [minimumLiquidity, setMinimumLiquidity] = useState("");
  const [maximumCapital, setMaximumCapital] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const linkedRoute = useRef("");

  useEffect(() => {
    const routeId = new URLSearchParams(location.search).get("opportunity");
    if (
      routeId &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(routeId)
    ) {
      linkedRoute.current = routeId.toLowerCase();
      setSelected(linkedRoute.current);
      setHistory(true);
      setInspectorOpen(true);
    }
  }, []);
  useEffect(() => {
    if (detail?.id === linkedRoute.current) {
      document.querySelector(".radar-inspector-container")?.scrollIntoView({ block: "start" });
    }
  }, [detail?.id]);

  useEffect(() => {
    const timer = setTimeout(() => setAssetQuery(asset.trim().toUpperCase()), 350);
    return () => clearTimeout(timer);
  }, [asset]);
  useEffect(() => {
    setOffset(0);
  }, [budget, assetQuery, exchange, status, sort, history, filters]);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.allSettled([
      read<SupportedBudgets>("/api/opportunities/budgets", controller.signal).then((value) => {
        setBudgets(value.budgets_cad);
        setBudget(value.default_budget_cad);
      }),
      read<{ slug: string; name: string; enabled: boolean }[]>(
        "/api/exchanges",
        controller.signal,
      ).then((value) => setExchanges(value.filter((item) => item.enabled))),
    ]);
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let running = false;
    setLoading(true);
    async function update() {
      if (running || document.visibilityState === "hidden") return;
      running = true;
      const query = new URLSearchParams({
        budget_cad: budget,
        limit: String(PAGE_SIZE),
        offset: String(offset),
        sort,
        history: String(history),
      });
      if (assetQuery) query.set("asset", assetQuery);
      if (exchange) query.set("exchange", exchange);
      if (status) query.set("status", status);
      Object.entries(filters).forEach(([key, value]) => {
        if (value) query.set(key, value);
      });
      try {
        const [rows, health] = await Promise.all([
          read<OpportunityPage>(`/api/opportunities?${query}`, controller.signal),
          read<OpportunityDiagnostics>("/api/opportunities/diagnostics", controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setPage(rows);
        setDiagnostics(health);
        setError("");
        setSelected((value) =>
          (linkedRoute.current && value === linkedRoute.current) ||
          rows.items.some((row) => row.id === value)
            ? value
            : rows.items[0]?.id || "",
        );
      } catch (error) {
        if (!controller.signal.aborted)
          setError(error instanceof Error ? error.message : "Opportunity data unavailable.");
      } finally {
        running = false;
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void update();
    const timer = setInterval(update, 10000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [budget, assetQuery, exchange, status, sort, history, offset, refresh, filters]);
  useEffect(() => {
    setDetail(null);
    setRecheckMessage("");
  }, [selected, budget]);
  useEffect(() => {
    setDetailError("");
    if (!selected) return;
    const controller = new AbortController();
    let running = false;
    async function update() {
      if (running || document.visibilityState === "hidden") return;
      running = true;
      try {
        const value = await read<Opportunity>(
          `/api/opportunities/${selected}?budget_cad=${encodeURIComponent(budget)}`,
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setDetail(value);
          setDetailError("");
        }
      } catch (error) {
        if (!controller.signal.aborted)
          setDetailError(
            error instanceof Error ? error.message : "Calculation details unavailable.",
          );
      } finally {
        running = false;
      }
    }
    void update();
    const timer = setInterval(update, 10000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [selected, budget, refresh]);

  async function recheck(routeId = selected) {
    if (!routeId) return;
    setRechecking(true);
    setRecheckMessage("");
    try {
      const response = await fetch(`/api/opportunities/${routeId}/recheck`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      const value = await response.json();
      if (!response.ok)
        throw new Error(
          typeof value.detail === "string" ? value.detail : "Recheck unavailable or rate limited.",
        );
      setRecheckMessage(
        "Fresh evidence requested. The result will update after the rate-limited collector responds.",
      );
      setRefresh((value) => value + 1);
    } catch (error) {
      setRecheckMessage(error instanceof Error ? error.message : "Recheck unavailable.");
    } finally {
      setRechecking(false);
    }
  }

  const metrics = diagnostics?.metrics;
  const clockUntrusted = diagnostics?.clock_healthy !== true;
  return (
    <div className="radar-workspace">
      <div className="section-heading radar-heading">
        <div>
          <span className="small-label">EXECUTABLE MARKET EVIDENCE</span>
          <h2>Cross-exchange radar</h2>
        </div>
        <span className="loading-label" role="status">
          {loading ? (
            <>
              <LoaderCircle size={13} className="spin" /> Updating
            </>
          ) : (
            "Refreshes every 10s"
          )}
        </span>
      </div>

      <div className="radar-summary">
        {[
          ["Monitored books", metrics?.monitored_markets],
          ["Compatible intersections", metrics?.compatible_intersections],
          ["Verified market", metrics?.verified_market],
          ["Research only", metrics?.research_only],
        ].map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value ?? "—"}</strong>
          </div>
        ))}
      </div>
      {clockUntrusted && (
        <div className="notice radar-warning" role="status">
          <AlertTriangle size={16} />
          <div>
            <strong>Clock confidence prevents actionable calculations</strong>
            <p>
              {diagnostics
                ? "UTC verification or host clock stability has not passed. Research observations retain their timing uncertainty."
                : "Clock diagnostics are not available. No live opportunity can be assumed trustworthy."}
            </p>
          </div>
        </div>
      )}
      {diagnostics?.healthy === false && (
        <p className="notice">
          The scanner is unavailable or its heartbeat is stale. Retained evidence may be historical.
        </p>
      )}
      <Button
        className="radar-filter-toggle"
        variant="outline"
        size="sm"
        onClick={() => setFiltersOpen(!filtersOpen)}
        aria-expanded={filtersOpen}
      >
        {filtersOpen ? "Hide filters" : "Filter Radar"}
      </Button>
      <div className={`radar-filters ${filtersOpen ? "filters-expanded" : ""}`}>
        <label>
          Capital budget · CAD
          <select value={budget} onChange={(event) => setBudget(event.target.value)}>
            {!(budgets.length ? budgets : ["1000"]).includes(budget) && (
              <option value={budget}>{decimal(budget)} · custom</option>
            )}
            {(budgets.length ? budgets : ["1000"]).map((value) => (
              <option value={value} key={value}>
                {decimal(value)}
              </option>
            ))}
          </select>
        </label>

        <label>
          Asset
          <input
            value={asset}
            maxLength={32}
            onChange={(event) => setAsset(event.target.value)}
            placeholder="All assets"
          />
        </label>
        <label>
          Exchange
          <select value={exchange} onChange={(event) => setExchange(event.target.value)}>
            <option value="">All exchanges</option>
            {exchanges.map((value) => (
              <option key={value.slug} value={value.slug}>
                {value.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Confidence
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All states</option>
            <option value="VERIFIED_MARKET">Verified market</option>
            <option value="RESEARCH_ONLY">Research only / anomalies</option>
            <option value="RECHECKING">Rechecking</option>
            <option value="INVALIDATED">Invalidated</option>
            <option value="EXPIRED">Expired</option>
          </select>
        </label>

        <label>
          Sort by
          <select value={sort} onChange={(event) => setSort(event.target.value)}>
            <option value="net_profit">Net CAD profit</option>
            <option value="net_return">Net return %</option>
          </select>
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={history}
            onChange={(event) => setHistory(event.target.checked)}
          />{" "}
          Include history
        </label>
      </div>
      <details className="radar-extra-controls">
        <summary>
          Advanced filters <small>Profit, liquidity, custom budget and book quality</small>
        </summary>
        <form
          className="terminal-filterbar radar-advanced-filters"
          onSubmit={(event) => {
            event.preventDefault();
            setFilters({
              ...filters,
              search: search.trim(),
              min_profit_cad: minimumProfit,
              min_return_pct: minimumReturn,
              min_quantity: minimumQuantity,
              min_liquidity_cad: minimumLiquidity,
              max_acquisition_cad: maximumCapital,
            });
          }}
        >
          <label>
            Search routes
            <input
              aria-label="Search opportunity routes"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              maxLength={80}
              placeholder="Asset, venue or symbol"
            />
          </label>
          {(
            [
              ["Minimum net CAD profit", minimumProfit, setMinimumProfit],
              ["Minimum net return %", minimumReturn, setMinimumReturn],
              ["Minimum base quantity", minimumQuantity, setMinimumQuantity],
              ["Minimum liquidity CAD", minimumLiquidity, setMinimumLiquidity],
              ["Maximum acquisition CAD", maximumCapital, setMaximumCapital],
            ] as const
          ).map(([label, value, set]) => (
            <label key={label}>
              {label}
              <input
                aria-label={label}
                inputMode="decimal"
                value={value}
                onChange={(e) => set(e.target.value)}
              />
            </label>
          ))}
          <Button variant="outline" size="sm" type="submit">
            Apply filters
          </Button>
          <Button
            variant="ghost"
            size="sm"
            type="button"
            onClick={() => {
              setSearch("");
              setMinimumProfit("");
              setMinimumReturn("");
              setMinimumQuantity("");
              setMinimumLiquidity("");
              setMaximumCapital("");
              setFilters({});
            }}
          >
            Clear extra filters
          </Button>
        </form>
        <div className="radar-extra-secondary">
          {" "}
          <form
            className="radar-custom-budget"
            onSubmit={(event) => {
              event.preventDefault();
              if (/^\d{1,7}(\.\d{1,2})?$/.test(customBudget) && /[1-9]/.test(customBudget))
                setBudget(customBudget);
            }}
          >
            <label>
              Custom CAD budget
              <input
                inputMode="decimal"
                value={customBudget}
                onChange={(event) => setCustomBudget(event.target.value)}
                placeholder="e.g. 750"
                pattern="[0-9]{1,7}(\.[0-9]{1,2})?"
              />
            </label>
            <Button type="submit" size="sm" variant="outline">
              Apply
            </Button>
          </form>{" "}
          <label>
            Data confidence
            <select
              aria-label="Data confidence"
              value={filters.confidence || ""}
              onChange={(event) =>
                setFilters((old) => ({ ...old, confidence: event.target.value }))
              }
            >
              <option value="">All book quality states</option>
              {["HEALTHY", "DEGRADED", "SUSPICIOUS", "STALE", "UNAVAILABLE"].map((value) => (
                <option key={value} value={value}>
                  {reasonLabel(value)}
                </option>
              ))}
            </select>
          </label>
        </div>
      </details>
      <p className="catalog-meta">
        Depth-aware pre-funded estimates include explicit costs. Missing conversion evidence is
        shown as unavailable. Long decimals use ≈; hover for exact values or open calculation
        evidence. Public API access does not establish Canadian or Alberta trading eligibility.
      </p>
      {error && (
        <p className="notice" role="alert">
          {error} Any retained rows below may be outdated.
        </p>
      )}
      {page.window_limited && (
        <p className="notice">
          Showing the most recent {page.candidate_window} matching routes from{" "}
          {page.matching_retained_routes} retained routes. Narrow the asset or exchange filter to
          inspect older evidence.
        </p>
      )}
      <div className="radar-mobile-cards" aria-label="Mobile opportunity summaries">
        {page.items.map((row) => (
          <article key={row.id}>
            <div className="opportunity-card-top">
              <strong>{row.base_asset}</strong>
              <Confidence value={row.status} />
            </div>
            <div className="opportunity-card-venues">
              <span>
                <small>BUY</small>
                {row.buy_exchange}
                <Amount value={row.buy_price} />
              </span>
              <ChevronRight size={17} />
              <span>
                <small>SELL</small>
                {row.sell_exchange}
                <Amount value={row.sell_price} />
              </span>
            </div>
            <div className="opportunity-card-profit">
              <div>
                <small>Expected net · CAD</small>
                <strong className={row.net_profit_cad?.startsWith("-") ? "value-negative" : ""}>
                  <Amount value={row.net_profit_cad} />
                </strong>
              </div>
              <div>
                <small>Net return</small>
                <strong className={row.net_return_pct?.startsWith("-") ? "value-negative" : ""}>
                  <Amount value={row.net_return_pct} suffix="%" />
                </strong>
              </div>
            </div>
            <p>
              <Amount value={row.quantity} /> base units · age{" "}
              {row.age_seconds === null || row.age_seconds === undefined
                ? "unavailable"
                : `${Math.max(0, Math.floor(row.age_seconds))}s`}{" "}
              · {reasonLabel(row.eligibility)}
            </p>
            <RouteSummary route={row} />
            <div className="paper-actions">
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setSelected(row.id);
                  setInspectorOpen(true);
                }}
              >
                Inspect evidence
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={rechecking}
                onClick={() => {
                  setSelected(row.id);
                  void recheck(row.id);
                }}
              >
                <RefreshCw size={13} /> Fresh recheck
              </Button>
            </div>
          </article>
        ))}
      </div>
      <div className="table-wrap feeds-table radar-table">
        <table aria-label="Cross-exchange opportunities">
          <thead>
            <tr>
              {[
                "Asset",
                "Buy / sell venues",
                "Weighted buy / sell",
                "Base quantity",
                "Net P&L · CAD",
                "Net return",
                "Buy capital · CAD",
                "Confidence / eligibility",
                "Detected · UTC",
              ].map((label) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {page.items.length ? (
              page.items.map((row) => (
                <tr key={row.id} className={selected === row.id ? "selected-feed" : ""}>
                  <td>
                    <button
                      className="exchange-select"
                      onClick={() => {
                        setSelected(row.id);
                        setInspectorOpen(true);
                        setRecheckMessage("");
                      }}
                      aria-label={`Inspect ${row.base_asset} ${row.buy_exchange} to ${row.sell_exchange}`}
                    >
                      <strong>{row.base_asset}</strong>
                      <small>
                        {row.buy_symbol} → {row.sell_symbol}
                      </small>
                    </button>
                  </td>
                  <td>
                    <span>{row.buy_exchange}</span>
                    <small className="eligibility-note">→ {row.sell_exchange}</small>
                  </td>
                  <td className="radar-number">
                    <Amount value={row.buy_price} />
                    <small className="eligibility-note">
                      <Amount value={row.sell_price} />
                    </small>
                  </td>
                  <td className="radar-number">
                    <Amount value={row.quantity} />
                  </td>
                  <td className="radar-number">
                    <Amount value={row.net_profit_cad} />
                  </td>
                  <td className="radar-number">
                    <Amount value={row.net_return_pct} suffix="%" />
                  </td>
                  <td className="radar-number">
                    <Amount value={row.acquisition_cost_cad} />
                  </td>
                  <td>
                    <Confidence value={row.status} />
                    <small className="eligibility-note">
                      {reasonLabel(row.eligibility)} ·{" "}
                      {row.actionable ? "eligible" : "not actionable"}
                    </small>
                    <RouteSummary route={row} />
                  </td>
                  <td className="timing-cell">
                    <time dateTime={row.detected_at}>{utc(row.detected_at)}</time>
                    {row.age_seconds !== undefined && row.age_seconds !== null && (
                      <small>
                        {row.age_seconds < 0
                          ? "Clock uncertainty"
                          : `${Math.floor(row.age_seconds)}s ago`}
                      </small>
                    )}
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={9}>
                  <div className="table-empty">
                    <Radar size={25} />
                    <strong>
                      {loading
                        ? "Reading current market evidence"
                        : error
                          ? "Opportunity data unavailable"
                          : "No matching opportunities"}
                    </strong>
                    <span>
                      {loading
                        ? "Waiting for the authenticated scanner API."
                        : "A positive spread is not guaranteed. The engine records real evidence and suppresses unsupported profitability."}
                    </span>
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="radar-pagination">
        <span>
          {page.total
            ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, page.total)} of ${page.total}`
            : "0 results"}
        </span>
        <div>
          <Button
            variant="ghost"
            size="sm"
            disabled={offset === 0 || loading}
            onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
          >
            <ChevronLeft size={14} /> Previous
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={offset + PAGE_SIZE >= page.total || loading}
            onClick={() => setOffset((value) => value + PAGE_SIZE)}
          >
            Next <ChevronRight size={14} />
          </Button>
        </div>
      </div>
      {detailError && (
        <p className="notice" role="alert">
          {detailError}
        </p>
      )}
      <div className={`radar-inspector-container ${inspectorOpen ? "inspector-open" : ""}`}>
        <Button
          className="inspector-close"
          variant="outline"
          size="sm"
          onClick={() => setInspectorOpen(false)}
          aria-label="Close opportunity inspector"
        >
          <X size={16} /> Close evidence
        </Button>
        {detail && (
          <Detail
            opportunity={detail}
            budget={budget}
            recheck={() => void recheck()}
            busy={rechecking}
            message={recheckMessage}
          />
        )}
      </div>
      {selected && !detail && !detailError && (
        <p className="loading-label">
          <LoaderCircle size={13} className="spin" /> Loading calculation evidence
        </p>
      )}
      <RecordDetails title="Scanner and UTC diagnostics" value={diagnostics} />
    </div>
  );
}
