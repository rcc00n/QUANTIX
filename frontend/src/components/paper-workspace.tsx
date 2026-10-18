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
function JsonEvidence({ value }: { value: unknown }) {
  return <pre className="paper-evidence">{JSON.stringify(value, null, 2)}</pre>;
}
function StrategyForm({
  session,
  options,
  onSaved,
}: {
  session: PaperSession;
  options: StrategyOptions | null;
  onSaved: () => Promise<void>;
}) {
  const [settings, setSettings] = useState<PaperStrategy>(session.settings);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    setSettings(session.settings);
    setMessage("");
  }, [session.id, session.settings]);
  function field(key: string, value: unknown) {
    setSettings((old) => ({ ...old, [key]: value, preset: "Custom" }));
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setMessage("");
    try {
      await paperFetch(`/paper/sessions/${session.id}/strategy`, {
        method: "PUT",
        body: JSON.stringify({
          ...settings,
          mode: session.classification === "RESEARCH" ? "RESEARCH_SHADOW" : settings.mode,
        }),
      });
      await onSaved();
      setMessage("Strategy version saved. Scheduled simulations retain their original version.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Strategy could not be saved.");
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="paper-detail">
      <div className="section-heading">
        <div>
          <span className="small-label">VERSIONED CONFIGURATION</span>
          <h2>Paper strategy</h2>
        </div>
        <span className="subtle-tag">
          {settings.preset} · {session.strategy_version_id.slice(0, 8)}
        </span>
      </div>
      <form onSubmit={save}>
        <div className="paper-form-grid">
          <label>
            Preset
            <select
              aria-label="Strategy preset"
              value={options?.presets[settings.preset] ? settings.preset : "Custom"}
              onChange={(e) => {
                const next = options?.presets[e.target.value];
                if (next?.mode === "RESEARCH_SHADOW" && session.classification !== "RESEARCH") {
                  setMessage("Use a separate research portfolio for Aggressive Research.");
                  return;
                }
                if (next) {
                  setSettings({
                    ...next,
                    mode: session.classification === "RESEARCH" ? "RESEARCH_SHADOW" : next.mode,
                  });
                  setMessage("");
                }
              }}
            >
              <option value="Custom">Custom</option>
              {Object.keys(options?.presets || {}).map((name) => (
                <option key={name}>{name}</option>
              ))}
            </select>
          </label>
          <label>
            Simulation mode
            <select
              aria-label="Simulation mode"
              value={settings.mode}
              onChange={(e) => field("mode", e.target.value)}
            >
              {session.classification === "RESEARCH" ? (
                <option value="RESEARCH_SHADOW">Research shadow</option>
              ) : (
                <>
                  <option value="PRE_FUNDED">Pre-funded arbitrage</option>
                  <option value="TRANSFER">Transfer-based simulation</option>
                </>
              )}
            </select>
          </label>
          {session.classification === "RESEARCH" && (
            <label>
              Research execution model
              <select
                value={settings.research_execution_model || "PRE_FUNDED"}
                onChange={(e) => field("research_execution_model", e.target.value)}
              >
                <option value="PRE_FUNDED">Pre-funded</option>
                <option value="TRANSFER">Transfer-based</option>
              </select>
            </label>
          )}
          <label>
            Starting capital · CAD
            <input
              aria-label="Starting capital CAD"
              inputMode="decimal"
              value={settings.starting_capital_cad}
              readOnly
              required
            />
          </label>
          <label>
            Trade budget / maximum acquisition · CAD
            <input
              aria-label="Trade amount CAD"
              inputMode="decimal"
              value={settings.trade_amount_cad}
              onChange={(e) => field("trade_amount_cad", e.target.value)}
              required
            />
          </label>
          <BuyCapitalControl
            label="Minimum simulated Buy Capital · CAD"
            value={
              typeof settings.min_buy_capital_cad === "string" ? settings.min_buy_capital_cad : "0"
            }
            onChange={(value) => field("min_buy_capital_cad", value)}
          />
          <label>
            Minimum projected profit · CAD
            <input
              aria-label="Minimum profit CAD"
              inputMode="decimal"
              value={settings.minimum_profit_cad}
              onChange={(e) => field("minimum_profit_cad", e.target.value)}
              required
            />
          </label>
          <label>
            Minimum projected return · %
            <input
              aria-label="Minimum profit percentage"
              inputMode="decimal"
              value={settings.minimum_profit_pct}
              onChange={(e) => field("minimum_profit_pct", e.target.value)}
              required
            />
          </label>
          <label>
            Threshold logic
            <select
              value={settings.threshold_logic}
              onChange={(e) => field("threshold_logic", e.target.value)}
            >
              <option value="AND">CAD and percentage</option>
              <option value="OR">CAD or percentage</option>
            </select>
          </label>
          <label>
            Reaction delay · seconds
            <input
              aria-label="Reaction delay seconds"
              type="number"
              min="0"
              max="3600"
              step="1"
              list="paper-reaction-delays"
              value={settings.reaction_delay_seconds}
              onChange={(e) => field("reaction_delay_seconds", e.target.valueAsNumber)}
              required
            />
            <datalist id="paper-reaction-delays">
              {(options?.reaction_delays || [5, 15, 30, 60, 120]).map((n) => (
                <option key={n} value={n} />
              ))}
            </datalist>
          </label>
          <label>
            Concurrent simulations
            <input
              aria-label="Maximum concurrent simulations"
              type="number"
              min="1"
              max="20"
              value={settings.maximum_concurrent}
              onChange={(e) => field("maximum_concurrent", e.target.valueAsNumber)}
              required
            />
          </label>
          <label>
            Allowed exchanges
            <input
              value={(settings.allowed_exchanges || []).join(", ")}
              placeholder="All supported exchanges"
              onChange={(e) =>
                field(
                  "allowed_exchanges",
                  e.target.value
                    .split(",")
                    .map((x) => x.trim())
                    .filter(Boolean),
                )
              }
            />
          </label>
          <label>
            Allowed assets
            <input
              value={(settings.allowed_assets || []).join(", ")}
              placeholder="All supported assets"
              onChange={(e) =>
                field(
                  "allowed_assets",
                  e.target.value
                    .split(",")
                    .map((x) => x.trim())
                    .filter(Boolean),
                )
              }
            />
          </label>
        </div>
        <fieldset className="strategy-risk-fields">
          <legend>Risk, liquidity and costs</legend>
          <div className="paper-form-grid">
            {(
              [
                ["minimum_liquidity_cad", "Minimum executable liquidity · CAD", "100"],
                ["maximum_capital_per_exchange_pct", "Maximum capital per venue · %", "75"],
                ["maximum_inventory_exposure_pct", "Maximum inventory exposure · %", "75"],
                ["maximum_loss_cad", "Maximum tolerated loss · CAD", "50"],
                ["taker_fee_rate", "Conservative taker fee · fraction", "0.006"],
                ["slippage_bps", "Adverse execution slippage · bps", "10"],
                ["rebalance_bps", "Projected rebalancing reserve · bps", "10"],
              ] as const
            ).map(([key, label, fallback]) => (
              <label key={key}>
                {label}
                <input
                  aria-label={label}
                  inputMode="decimal"
                  value={String(settings[key] ?? fallback)}
                  onChange={(e) => field(key, e.target.value)}
                  required
                />
              </label>
            ))}
            <label>
              Maximum acceptable slippage · bps
              <input
                aria-label="Maximum acceptable slippage bps"
                inputMode="decimal"
                value={String(settings.maximum_slippage_bps ?? "")}
                placeholder="Additional cap unset"
                onChange={(e) => field("maximum_slippage_bps", e.target.value || null)}
              />
            </label>
            <label>
              Maximum simulations per hour
              <input
                type="number"
                min={1}
                max={1000}
                step={1}
                value={Number(settings.maximum_simulations_per_hour ?? 60)}
                onChange={(e) => field("maximum_simulations_per_hour", e.target.valueAsNumber)}
                required
              />
            </label>
            <label>
              Maximum book age · seconds
              <input
                type="number"
                min={1}
                max={60}
                step={1}
                value={Number(settings.maximum_book_age_seconds ?? 30)}
                onChange={(e) => field("maximum_book_age_seconds", e.target.valueAsNumber)}
                required
              />
            </label>
            <label>
              Execution profile
              <select
                value={String(settings.execution_profile ?? "MANUAL")}
                onChange={(e) => field("execution_profile", e.target.value)}
              >
                <option value="MANUAL">Human reaction scenario</option>
                <option value="FAST_COUNTERFACTUAL">Fast counterfactual scenario</option>
                <option value="CUSTOM">Custom scenario</option>
              </select>
            </label>
            <label>
              Required data confidence
              <select
                value={String(settings.minimum_confidence ?? "HEALTHY")}
                onChange={(e) => field("minimum_confidence", e.target.value)}
              >
                <option value="HEALTHY">Healthy observations</option>
                {session.classification === "RESEARCH" && (
                  <>
                    <option value="DEGRADED">Degraded · research only</option>
                    <option value="ANOMALY_RESEARCH">Anomalies · research only</option>
                  </>
                )}
              </select>
            </label>
            {(["buy_fee_currency", "sell_fee_currency"] as const).map((key) => (
              <label key={key}>
                {key === "buy_fee_currency" ? "Buy fee currency" : "Sell fee currency"}
                <select
                  value={String(settings[key] ?? "quote")}
                  onChange={(e) => field(key, e.target.value)}
                >
                  <option value="quote">Quote currency</option>
                  <option value="base">Base asset</option>
                </select>
              </label>
            ))}
          </div>
          <p className="paper-help">
            Taker fee rates are fractions: 0.006 means 0.6%. Costs remain assumptions until
            independently verified. Slippage caps apply to depth plus the adverse model; loss limits
            cannot guarantee future execution prices.
          </p>
        </fieldset>
        {(settings.mode === "TRANSFER" || settings.research_execution_model === "TRANSFER") && (
          <fieldset className="strategy-risk-fields">
            <legend>Transfer scenario assumptions</legend>
            <div className="paper-form-grid">
              {(
                [
                  ["network", "Compatible network", ""],
                  ["withdrawal_fee_base", "Withdrawal fee · base units", "0"],
                  ["network_fee_base", "Network fee · base units", "0"],
                  ["haircut_pct", "Pretrade transfer risk haircut · %", "5"],
                ] as const
              ).map(([key, label, fallback]) => (
                <label key={key}>
                  {label}
                  <input
                    value={String(asRecord(settings.transfer)[key] ?? fallback)}
                    onChange={(e) =>
                      field("transfer", { ...asRecord(settings.transfer), [key]: e.target.value })
                    }
                  />
                </label>
              ))}
              {(
                [
                  ["confirmations", "Confirmation assumption", 0],
                  ["settlement_seconds", "Settlement delay · seconds", 600],
                ] as const
              ).map(([key, label, fallback]) => (
                <label key={key}>
                  {label}
                  <input
                    type="number"
                    min={key === "settlement_seconds" ? 1 : 0}
                    step={1}
                    value={Number(asRecord(settings.transfer)[key] ?? fallback)}
                    onChange={(e) =>
                      field("transfer", {
                        ...asRecord(settings.transfer),
                        [key]: e.target.valueAsNumber,
                      })
                    }
                  />
                </label>
              ))}
              {(
                [
                  ["withdrawal_enabled", "Assume withdrawals available"],
                  ["deposit_enabled", "Assume deposits available"],
                ] as const
              ).map(([key, label]) => (
                <label className="checkbox-label" key={key}>
                  <input
                    type="checkbox"
                    checked={asRecord(settings.transfer)[key] === true}
                    onChange={(e) =>
                      field("transfer", { ...asRecord(settings.transfer), [key]: e.target.checked })
                    }
                  />
                  {label}
                </label>
              ))}
            </div>
            <p className="paper-help">
              These are explicit scenario assumptions, not verified blockchain or account
              capabilities. A later sell requires new observed depth after settlement.
            </p>
          </fieldset>
        )}
        <p className="paper-help">
          Minimum Buy Capital checks the actual executable purchase size independently of profit,
          return and the maximum budget. Legacy versions retain their original minimum until saved.
          Starting capital applies when creating a portfolio; saving a strategy never grants assets
          or resets balances. Reaction profiles are simulations of human or automated timing, not
          measured exchange latency.
        </p>
        {settings.mode === "TRANSFER" || settings.research_execution_model === "TRANSFER" ? (
          <Notice>
            Transfer simulations require explicit compatible networks, fees and settlement
            assumptions. The eventual sale uses newly available evidence after arrival.
          </Notice>
        ) : null}
        <div className="paper-actions">
          <Button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save strategy version"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              const defaults =
                options?.presets[
                  session.classification === "RESEARCH" ? "Aggressive Research" : "Balanced"
                ];
              if (defaults) {
                setSettings({ ...defaults, starting_capital_cad: session.initial_equity_cad });
                setMessage(
                  "Defaults loaded. Save a new version to apply; scheduled simulations keep their original settings.",
                );
              }
            }}
          >
            Reset strategy defaults
          </Button>
          {message && <span role="status">{message}</span>}
        </div>
        <details className="paper-disclosure">
          <summary>Complete strategy and risk limits</summary>
          <JsonEvidence value={settings} />
        </details>
      </form>
    </section>
  );
}
function CreatePortfolio({
  options,
  onCreated,
  onClose,
  onPlan,
}: {
  options: StrategyOptions | null;
  onCreated: () => Promise<void>;
  onClose: () => void;
  onPlan: (capital: string, name: string) => void;
}) {
  const [name, setName] = useState("");
  const [capital, setCapital] = useState("10000");
  const [classification, setClassification] = useState<PaperClassification>("RESEARCH");
  const [allocations, setAllocations] = useState<Array<{ exchange: string; quantity: string }>>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (classification === "RESEARCH") {
      onPlan(capital, name.trim());
      return;
    }
    setError("");
    setSaving(true);
    try {
      const preset = options?.presets.Balanced;
      await paperFetch("/paper/sessions", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          classification,
          starting_capital_cad: capital,
          ...(preset ? { settings: { ...preset, starting_capital_cad: capital } } : {}),
          ...(allocations.length
            ? {
                allocations: allocations.map((allocation) => ({
                  ...allocation,
                  asset: "fiat:CAD",
                  cad_rate: "1",
                })),
              }
            : {}),
        }),
      });
      await onCreated();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Portfolio could not be created.");
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="paper-detail paper-create">
      <div className="section-heading">
        <h2>Create paper portfolio</h2>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close portfolio form">
          <X size={16} />
        </Button>
      </div>
      <form onSubmit={submit}>
        <div className="paper-form-grid">
          <label>
            Portfolio name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              minLength={1}
              maxLength={100}
              required
            />
          </label>
          <label>
            Classification
            <select
              value={classification}
              onChange={(e) => setClassification(e.target.value as PaperClassification)}
            >
              <option value="RESEARCH">Research-only shadow</option>
              <option value="VALIDATED">Validated paper portfolio</option>
            </select>
          </label>
          <label>
            Starting equity · CAD
            <input
              inputMode="decimal"
              value={capital}
              onChange={(e) => setCapital(e.target.value)}
              required
            />
          </label>
        </div>
        {classification === "RESEARCH" ? (
          <p className="paper-help">
            Review an immutable allocation proposal before creating this research portfolio.
          </p>
        ) : (
          <>
            <p className="paper-help">
              Default validated funding is split in CAD between Kraken and NDAX.
            </p>
            <details className="paper-disclosure">
              <summary>Set initial CAD inventories</summary>
              <p className="paper-help">
                Custom CAD positions must total the exact starting equity.
              </p>
              {allocations.map((allocation, index) => (
                <div className="paper-allocation" key={index}>
                  <label>
                    Exchange
                    <input
                      aria-label={`Allocation ${index + 1} exchange`}
                      value={allocation.exchange}
                      onChange={(e) =>
                        setAllocations((old) =>
                          old.map((a, i) => (i === index ? { ...a, exchange: e.target.value } : a)),
                        )
                      }
                      required
                    />
                  </label>
                  <label>
                    CAD quantity
                    <input
                      aria-label={`Allocation ${index + 1} CAD quantity`}
                      inputMode="decimal"
                      value={allocation.quantity}
                      onChange={(e) =>
                        setAllocations((old) =>
                          old.map((a, i) => (i === index ? { ...a, quantity: e.target.value } : a)),
                        )
                      }
                      required
                    />
                  </label>
                  <label>
                    CAD per unit · identity
                    <input aria-label={`Allocation ${index + 1} CAD rate`} value="1" readOnly />
                  </label>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => setAllocations((old) => old.filter((_, i) => i !== index))}
                    aria-label={`Remove allocation ${index + 1}`}
                  >
                    <X size={14} />
                  </Button>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  setAllocations((old) => [...old, { exchange: "kraken", quantity: "" }])
                }
              >
                <Plus size={14} /> Add allocation
              </Button>
            </details>
          </>
        )}
        <div className="paper-actions">
          <Button type="submit" disabled={saving}>
            {classification === "RESEARCH"
              ? "Open funding planner"
              : saving
                ? "Creating…"
                : "Create virtual portfolio"}
          </Button>
          {error && <span role="alert">{error}</span>}
        </div>
      </form>
    </section>
  );
}
function Balances({ portfolio }: { portfolio: PaperPortfolio | null }) {
  return (
    <section className="paper-detail">
      <div className="section-heading">
        <div>
          <span className="small-label">LEDGER-BACKED INVENTORY</span>
          <h2>Virtual exchange balances</h2>
        </div>
      </div>
      <div className="table-wrap">
        <table className="paper-table" aria-label="Virtual balances">
          <thead>
            <tr>
              {[
                "Exchange",
                "Asset",
                "Available",
                "Reserved",
                "Total inventory",
                "CAD cost basis",
                "CAD valuation",
              ].map((label) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {portfolio?.balances?.length ? (
              portfolio.balances.map((balance) => (
                <tr key={`${balance.exchange}:${balance.asset}`}>
                  <td>{balance.exchange}</td>
                  <td>{assetLabel(balance.asset)}</td>
                  <td>
                    <Exact value={balance.available_quantity} />
                  </td>
                  <td>
                    <Exact value={balance.reserved_quantity} />
                  </td>
                  <td>
                    <Exact value={balance.quantity} />
                  </td>
                  <td>
                    <Exact value={balance.cost_basis_cad} />
                  </td>
                  <td>
                    <Exact value={balance.market_value_cad} />
                    {balance.valuation_detail_reason && (
                      <small className="paper-valuation-note">
                        {balance.valuation_detail_reason.replaceAll("_", " ")}
                      </small>
                    )}
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={7}>
                  <div className="table-empty">
                    <strong>No virtual balances available</strong>
                    <span>Funding and valuation require an initialized paper portfolio.</span>
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
function PortfolioDetails({ portfolio }: { portfolio: PaperPortfolio | null }) {
  return (
    <section className="paper-detail">
      <div className="section-heading">
        <div>
          <span className="small-label">ACCOUNTING BREAKDOWN</span>
          <h2>Inventory and strategy performance</h2>
        </div>
      </div>
      <div className="paper-accounting-grid">
        <dl className="settings-list">
          {[
            [
              "Realized simulated trading P&L",
              portfolio?.realized_trading_pnl_cad ?? portfolio?.realized_trading_cad,
            ],
            ["Realized currency P&L", portfolio?.realized_fx_pnl_cad ?? portfolio?.realized_fx_cad],
            ["Unrealized inventory P&L", portfolio?.unrealized_pnl_cad],
            ["Trading fees", portfolio?.fees_cad],
            ["FX, slippage and rebalancing expenses", portfolio?.expenses_cad],
          ].map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                <Exact value={value} unit="CAD" />
              </dd>
            </div>
          ))}
        </dl>
        <dl className="settings-list">
          {[
            ["No-trading benchmark", portfolio?.no_trading_benchmark_cad, "CAD"],
            ["Strategy excess versus benchmark", portfolio?.strategy_excess_cad, "CAD"],
            ["Portfolio return", portfolio?.portfolio_return_pct, "%"],
            ["Capital utilization", portfolio?.capital_utilization_pct, "%"],
          ].map(([label, value, unit]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                <Exact value={value} unit={unit || "CAD"} />
              </dd>
            </div>
          ))}
        </dl>
      </div>
      <p className="paper-help">
        Fees and expenses are disclosed separately and are already reflected in balances and cost
        basis. They are not deducted twice. The benchmark retains initial inventory without trading.
      </p>
    </section>
  );
}
export function PaperWorkspace({ view }: { view: "trading" | "portfolio" | "settings" }) {
  const [sessions, setSessions] = useState<PaperSession[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [options, setOptions] = useState<StrategyOptions | null>(null);
  const [portfolioState, setPortfolio] = useState<PaperPortfolio | null>(null);
  const [metricsState, setMetrics] = useState<PaperMetrics | null>(null);
  const [tradesState, setTrades] = useState<PaperTradePage | null>(null);
  const [offset, setOffset] = useState(0);
  const [detailState, setDetail] = useState<PaperTradeDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [showFunding, setShowFunding] = useState(false);
  const [fundingDraft, setFundingDraft] = useState<{ capital: string; name: string }>();
  const scopeRef = useRef(sessionId);
  scopeRef.current = sessionId;
  const [loadedScope, setLoadedScope] = useState("");
  const portfolio = loadedScope === sessionId ? portfolioState : null;
  const metrics = loadedScope === sessionId ? metricsState : null;
  const trades = loadedScope === sessionId ? tradesState : null;
  const detail = loadedScope === sessionId ? detailState : null;
  const session = sessions.find((item) => item.id === sessionId) || null;
  useEffect(() => {
    if (!showFunding) return;
    const frame = requestAnimationFrame(() =>
      document.getElementById("virtual-funding-planner")?.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      }),
    );
    return () => cancelAnimationFrame(frame);
  }, [showFunding, sessionId, fundingDraft]);
  const loadSessions = useCallback(async () => {
    const [response, strategies] = await Promise.all([
      paperFetch<{ items: PaperSession[] }>("/paper/sessions"),
      paperFetch<StrategyOptions>("/strategies"),
    ]);
    setSessions(response.items);
    setOptions(strategies);
    const requested = new URLSearchParams(window.location.search).get("session");
    setSessionId((old) =>
      response.items.some((s) => s.id === old)
        ? old
        : response.items.find((s) => s.id === requested)?.id ||
          response.items.find((s) => s.classification === "VALIDATED")?.id ||
          response.items[0]?.id ||
          "",
    );
  }, []);
  useEffect(() => {
    loadSessions()
      .catch((reason) => setError(reason.message))
      .finally(() => setLoading(false));
  }, [loadSessions]);
  useEffect(() => {
    if (!sessionId) return;
    let active = true;
    let running = false;
    const controller = new AbortController();
    setPortfolio(null);
    setMetrics(null);
    setTrades(null);
    setDetail(null);
    async function refresh() {
      if (running || document.visibilityState === "hidden") return;
      running = true;
      try {
        const [value, activity, page] = await Promise.all([
          paperFetch<PaperPortfolio>(`/paper/sessions/${sessionId}/portfolio`, {
            signal: controller.signal,
          }),
          paperFetch<PaperMetrics>(`/paper/sessions/${sessionId}/metrics`, {
            signal: controller.signal,
          }),
          paperFetch<PaperTradePage>(
            `/paper/trades?session_id=${sessionId}&limit=25&offset=${offset}`,
            { signal: controller.signal },
          ),
        ]);
        if (active) {
          setLoadedScope(sessionId);
          setPortfolio(value);
          setMetrics(activity);
          setTrades(page);
          setError("");
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "Paper data unavailable.");
      } finally {
        running = false;
      }
    }
    void refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [sessionId, offset]);
  async function toggle() {
    if (!session) return;
    setBusy(true);
    setError("");
    try {
      await paperFetch(
        `/paper/sessions/${session.id}/${session.status.toLowerCase() === "active" ? "pause" : "resume"}`,
        { method: "POST", body: "{}" },
      );
      await loadSessions();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Portfolio status could not change.");
    } finally {
      setBusy(false);
    }
  }
  async function openTrade(trade: PaperTrade) {
    try {
      const requestedScope = sessionId;
      const value = await paperFetch<PaperTradeDetail>(`/paper/trades/${trade.id}`);
      if (scopeRef.current === requestedScope) setDetail(value);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Execution evidence unavailable.");
    }
  }
  return (
    <div className="paper-workspace">
      <div className="section-heading paper-heading">
        <div>
          <span className="small-label">AUTONOMOUS SIMULATION</span>
          <h2>
            {view === "portfolio"
              ? "Portfolio accounting"
              : view === "settings"
                ? "Strategy management"
                : "Paper trading activity"}
          </h2>
        </div>
        <div className="paper-actions">
          <Button
            variant="outline"
            size="sm"
            aria-expanded={showFunding}
            aria-controls="virtual-funding-planner"
            onClick={() => setShowFunding(!showFunding)}
          >
            {showFunding ? "Hide funding planner" : "Virtual Funding Planner"}
          </Button>
          <Button variant="outline" size="sm" onClick={() => setCreating(!creating)}>
            <Plus size={14} /> New portfolio
          </Button>
        </div>
      </div>
      <Notice>
        Virtual funds and simulated executions only. Research results are excluded from validated
        performance. Real exchange orders are never submitted.
      </Notice>
      {error && (
        <div className="notice" role="alert">
          {error}{" "}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void loadSessions().catch((reason) => setError(reason.message))}
          >
            <RefreshCw size={13} /> Retry
          </Button>
        </div>
      )}
      {creating && (
        <CreatePortfolio
          options={options}
          onCreated={loadSessions}
          onClose={() => setCreating(false)}
          onPlan={(capital, name) => {
            setFundingDraft({ capital, name });
            setCreating(false);
            setShowFunding(true);
          }}
        />
      )}
      {loading ? (
        <p className="empty-line" role="status">
          Loading paper portfolios…
        </p>
      ) : sessions.length ? (
        <>
          <div className="paper-toolbar">
            <label>
              Paper portfolio
              <select
                aria-label="Paper portfolio"
                value={sessionId}
                onChange={(e) => {
                  setSessionId(e.target.value);
                  setOffset(0);
                }}
              >
                {sessions.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ·{" "}
                    {item.classification === "RESEARCH" ? "Research only" : "Validated"}
                  </option>
                ))}
              </select>
            </label>
            {session && (
              <>
                <Classification value={session.classification} />
                <span className="subtle-tag">{session.status}</span>
                <Button variant="outline" size="sm" disabled={busy} onClick={toggle}>
                  {session.status.toLowerCase() === "active" ? (
                    <CirclePause size={14} />
                  ) : (
                    <Play size={14} />
                  )}
                  {session.status.toLowerCase() === "active"
                    ? "Pause simulations"
                    : "Resume simulations"}
                </Button>
              </>
            )}
          </div>
          {session && (
            <>
              <Summary session={session} portfolio={portfolio} metrics={metrics} />
              {showFunding && (
                <FundingPlanner
                  key={`${session.id}:${fundingDraft?.capital ?? ""}:${fundingDraft?.name ?? ""}`}
                  session={session}
                  onCreated={loadSessions}
                  initialCapital={fundingDraft?.capital}
                  initialName={fundingDraft?.name}
                />
              )}
              {view === "settings" ? (
                <StrategyForm
                  key={session.id}
                  session={session}
                  options={options}
                  onSaved={loadSessions}
                />
              ) : view === "portfolio" ? (
                <>
                  <PortfolioDetails portfolio={portfolio} />
                  <Balances portfolio={portfolio} />
                </>
              ) : (
                <>
                  <div className="paper-counts" aria-label="Simulation outcomes">
                    {[
                      ["scheduled", metrics?.scheduled_simulations],
                      ["completed", metrics?.completed_simulations],
                      ["partial", metrics?.partially_completed],
                      ["missed", metrics?.missed_simulations],
                      ["failed", metrics?.failed_simulations],
                      ["expired", metrics?.expired_simulations],
                    ].map(([key, count]) => (
                      <div key={key}>
                        <span>{key}</span>
                        <strong>{metrics ? (count ?? 0) : "—"}</strong>
                      </div>
                    ))}
                    <div>
                      <span>Win rate</span>
                      <strong>
                        <Exact value={metrics?.win_rate_pct} unit="%" />
                      </strong>
                    </div>
                  </div>
                  <ExecutionReasons metrics={metrics} />
                  <div className="section-heading paper-ledger-heading">
                    <h3>Simulation history</h3>
                    <Link href={`/portfolio?session=${session.id}`} className="text-link">
                      Inspect virtual balances <ChevronRight size={14} />
                    </Link>
                  </div>
                  <div className="table-wrap">
                    <table className="paper-table" aria-label="Paper simulations">
                      <thead>
                        <tr>
                          {[
                            "Simulation",
                            "Classification",
                            "Mode",
                            "Lifecycle",
                            "Requested quantity",
                            "Eligible after",
                            "Outcome reason",
                          ].map((label) => (
                            <th key={label}>{label}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {trades?.items?.length ? (
                          trades.items.map((trade) => (
                            <tr key={trade.id}>
                              <td>
                                <button
                                  className="paper-row-link"
                                  onClick={() => void openTrade(trade)}
                                >
                                  {trade.id.slice(0, 8)}
                                </button>
                              </td>
                              <td>
                                <Classification value={trade.classification} />
                              </td>
                              <td>{trade.mode.replaceAll("_", " ")}</td>
                              <td>
                                <span className={`paper-state state-${trade.status.toLowerCase()}`}>
                                  {trade.status}
                                </span>
                              </td>
                              <td>
                                <Exact value={trade.requested_quantity} />
                              </td>
                              <td>{utcLabel(trade.eligible_at)}</td>
                              <td className="paper-reason">
                                {trade.reason || "Awaiting next lifecycle transition"}
                              </td>
                            </tr>
                          ))
                        ) : (
                          <tr>
                            <td colSpan={7}>
                              <div className="table-empty">
                                <strong>
                                  {trades
                                    ? "No recorded simulations"
                                    : "Loading simulation history…"}
                                </strong>
                                <span>
                                  Eligible candidates require virtual funding and causally valid
                                  market evidence. Empty results never imply a successful trade.
                                </span>
                              </div>
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                  <div className="radar-pagination">
                    <span>{trades?.total ?? 0} recorded simulations · UTC timestamps</span>
                    <div>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={offset === 0}
                        onClick={() => setOffset(Math.max(0, offset - 25))}
                      >
                        <ChevronLeft size={13} /> Previous
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!trades || offset + 25 >= trades.total}
                        onClick={() => setOffset(offset + 25)}
                      >
                        Next <ChevronRight size={13} />
                      </Button>
                    </div>
                  </div>
                  {detail && (
                    <section className="paper-detail">
                      <div className="section-heading">
                        <h3>Execution evidence · {detail.trade.id.slice(0, 8)}</h3>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            const url = URL.createObjectURL(
                              new Blob([JSON.stringify(detail, null, 2)], {
                                type: "application/json",
                              }),
                            );
                            const link = document.createElement("a");
                            link.href = url;
                            link.download = `quantix-paper-${detail.trade.id}.json`;
                            link.click();
                            URL.revokeObjectURL(url);
                          }}
                        >
                          Download JSON evidence
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setDetail(null)}
                          aria-label="Close execution evidence"
                        >
                          <X size={15} />
                        </Button>
                      </div>
                      <Notice>
                        Every fill is virtual. Missing evidence produces missed or failed outcomes;
                        the two exchange legs are independent.
                      </Notice>
                      <TradeInspector detail={detail} />
                      <details className="paper-disclosure">
                        <summary>Remaining exposure and complete simulation</summary>
                        <JsonEvidence value={detail.trade} />
                      </details>
                    </section>
                  )}
                </>
              )}
            </>
          )}
        </>
      ) : (
        <>
          <div className="paper-empty">
            <h3>No paper portfolios initialized</h3>
            <p>Create a portfolio to allocate virtual capital.</p>
          </div>
          {showFunding && (
            <FundingPlanner
              session={null}
              onCreated={loadSessions}
              initialCapital={fundingDraft?.capital}
              initialName={fundingDraft?.name}
            />
          )}
        </>
      )}
    </div>
  );
}

export function PaperOverview() {
  const [session, setSession] = useState<PaperSession | null>(null);
  const [portfolio, setPortfolio] = useState<PaperPortfolio | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const list = await paperFetch<{ items: PaperSession[] }>("/paper/sessions");
        const primary = list.items.find((x) => x.classification === "VALIDATED");
        if (primary) {
          const value = await paperFetch<PaperPortfolio>(`/paper/sessions/${primary.id}/portfolio`);
          if (active) {
            setSession(primary);
            setPortfolio(value);
          }
        }
      } finally {
        if (active) setLoaded(true);
      }
    }
    void load().catch(() => {});
    return () => {
      active = false;
    };
  }, []);
  return (
    <>
      <div className="metrics-strip">
        {[
          [
            "Validated portfolio equity",
            portfolio?.equity_cad,
            session
              ? session.name
              : loaded
                ? "No validated portfolio available"
                : "Loading portfolio",
          ],
          [
            "Realized simulated P&L",
            portfolio?.realized_trading_pnl_cad ?? portfolio?.realized_trading_cad,
            "Research results excluded",
          ],
          [
            "Unrealized inventory P&L",
            portfolio?.unrealized_pnl_cad,
            "Observed valuation evidence required",
          ],
          ["Capital utilization", portfolio?.capital_utilization_pct, "Reserved virtual inventory"],
        ].map(([label, value, note]) => (
          <div className="metric" key={label}>
            <span>{label}</span>
            <div>
              <Exact value={value} unit={label === "Capital utilization" ? "%" : "CAD"} />
            </div>
            <p>{note}</p>
          </div>
        ))}
      </div>
    </>
  );
}
