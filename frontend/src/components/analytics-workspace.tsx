"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Filter, ShieldCheck } from "lucide-react";
import { FullExport } from "@/components/full-export";
import { StrategyLab } from "@/components/strategy-lab";
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
import {
  paperSessionsPath,
  preferredPaperSession,
  utcLabel,
  type PaperSession,
  type PaperSessionScope,
  type PaperPortfolio,
} from "@/lib/paper";

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
export function AnalyticsWorkspace() {
  const [scope, setScope] = useState<PaperSessionScope>("current");
  const archived = scope === "archived";
  const sessions = useLive<{ items: PaperSession[] }>(paperSessionsPath(scope), 60000);
  const [sessionId, setSessionId] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [strategy, setStrategy] = useState("");
  const [query, setQuery] = useState("");
  const [formError, setFormError] = useState("");
  useEffect(() => {
    if (new URLSearchParams(location.search).get("scope") === "archived") {
      setScope("archived");
    }
  }, []);
  useEffect(() => {
    if (sessions.data && !sessions.data.items.some((s) => s.id === sessionId)) {
      const requested = new URLSearchParams(location.search).get("session");
      setSessionId(preferredPaperSession(sessions.data.items, requested)?.id ?? "");
    }
  }, [sessions.data, sessionId]);
  const session = sessions.data?.items.find((s) => s.id === sessionId);
  const selected = session?.id;
  const suffix = query ? `&${query}` : "";
  const live = useLive<AnalyticsResult>(
    selected ? `/analytics/sessions/${selected}?${query}` : null,
    30000,
  );
  const series = useLive<SeriesResult>(
    selected ? `/analytics/sessions/${selected}/series?limit=240${suffix}` : null,
    30000,
  );
  const m = live.data?.metrics || {};
  const p = live.data?.portfolio;
  const groups = [
    ["Exchange routes", "by_route"],
    ["Assets", "by_asset"],
    ["Strategies", "by_strategy"],
    ["Reaction delays", "by_reaction_delay_ms"],
  ] as const;
  function apply(event: FormEvent) {
    event.preventDefault();
    setFormError("");
    try {
      const q = new URLSearchParams();
      if (start) q.set("start", new Date(`${start}Z`).toISOString());
      if (end) q.set("end", new Date(`${end}Z`).toISOString());
      if (start && end && q.get("start")! >= q.get("end")!)
        throw new Error("Period end must follow start.");
      if (strategy) q.set("strategy_version_id", strategy);
      setQuery(q.toString());
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : "Invalid UTC period");
    }
  }
  const counts = asRecord(m.counts);
  const outcomes = Object.entries(counts).map(([label, value]) => ({ label, value }));
  const routes = asRecords(m.by_route).map((x) => ({
    ...x,
    label: text(x.route || x.buy_exchange || x.group || x.key, "Route"),
    value:
      x.net_simulated_pnl_cad || x.net_pnl_cad || x.net_completed_trade_pnl_cad || x.net_profit_cad,
  }));
  const expense = asRecord(live.data?.expense_breakdown);
  const expenses = [
    { label: "Trading fees", value: expense.trading_fees_cad ?? p?.fees_cad },
    { label: "Transfers / rebalancing", value: expense.transfer_rebalancing_expenses_cad },
    { label: "FX conversion", value: expense.fx_conversion_costs_cad },
  ];
  const exportQuery = new URLSearchParams(query);
  if (selected) exportQuery.set("session_id", selected);
  return (
    <div className="terminal-workspace analytics-workspace">
      <FullExport custom={selected ? Object.fromEntries(exportQuery) : null} />
      {!archived && (
        <StrategyLab
          sessions={sessions.data?.items ?? []}
          loading={sessions.loading}
          sessionError={sessions.error}
        />
      )}
      <div className="terminal-workspace-top">
        <div>
          <span className="small-label">NUMERICAL ACCOUNTING</span>
          <p className="terminal-scope-note">
            One independent portfolio · exact financial records · America/Edmonton daily reporting
          </p>
        </div>
        <LiveStatus {...live} interval={30} />
      </div>
      <form className="terminal-filterbar analytics-filterbar" onSubmit={apply}>
        <label>
          {archived ? "Archived portfolio" : "Paper portfolio"}
          <select
            aria-label="Analytics portfolio"
            value={sessionId}
            onChange={(e) => {
              setSessionId(e.target.value);
              setStrategy("");
              setQuery("");
            }}
          >
            {sessions.data?.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} · {s.classification}
              </option>
            ))}
          </select>
        </label>
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            setScope(archived ? "current" : "archived");
            setSessionId("");
            setStart("");
            setEnd("");
            setStrategy("");
            setQuery("");
            setFormError("");
          }}
        >
          {archived ? "Current portfolios" : "Archived"}
        </Button>
        <label>
          Period start · UTC
          <input
            aria-label="Report period start UTC"
            type="datetime-local"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          Period end · UTC
          <input
            aria-label="Report period end UTC"
            type="datetime-local"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
        <label>
          Strategy version
          <select
            aria-label="Report strategy version"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
          >
            <option value="">All versions</option>
            {(live.data?.strategy_versions || asRecords(m.by_strategy)).map((s, i) => (
              <option
                key={text(s.id || s.strategy_version_id, String(i))}
                value={text(s.id || s.strategy_version_id, "")}
              >
                {s.version
                  ? `Version ${s.version}`
                  : text(s.id || s.strategy_version_id).slice(0, 8)}
              </option>
            ))}
            {session &&
              !live.data?.strategy_versions?.length &&
              !asRecords(m.by_strategy).some(
                (x) => x.strategy_version_id === session.strategy_version_id,
              ) && (
                <option value={session.strategy_version_id}>
                  Current · {session.strategy_version_id.slice(0, 8)}
                </option>
              )}
          </select>
        </label>
        <Button type="submit" variant="outline">
          <Filter size={14} /> Apply period
        </Button>
      </form>
      <p className="terminal-footnote">
        Filters below affect portfolio analytics and the optional custom export. Export Everything
        always includes all retained data without these filters.
      </p>
      {archived && !sessions.loading && !sessions.data?.items.length && !sessions.error && (
        <Empty title="No archived portfolios">Archived accounting remains available here.</Empty>
      )}
      {sessions.error && <Warning>{sessions.error}</Warning>}
      {formError && <Warning>{formError}</Warning>}
      {(live.error || series.error) && <Warning>{live.error || series.error}</Warning>}
      {session?.classification === "RESEARCH" && (
        <Warning>
          Research-only experiment. Its starting capital and hypothetical outcomes never enter
          validated performance.
        </Warning>
      )}
      {strategy && (
        <Warning>
          Trade activity is filtered by strategy version. Portfolio equity and its benchmark remain
          the whole independent session.
        </Warning>
      )}
      <div className="terminal-kpi-grid">
        <Kpi
          label="Initial capital"
          value={p?.initial_equity_cad || session?.initial_equity_cad}
          unit="CAD"
        />
        <Kpi label="Current equity" value={p?.equity_cad} unit="CAD" />
        <Kpi label="Portfolio return" value={p?.portfolio_return_pct} unit="%" color />
        <Kpi
          label="Strategy excess"
          value={p?.strategy_excess_cad}
          unit="CAD"
          color
          help="Equity minus the no-trading benchmark for the original funded holdings."
        />
      </div>
      <div className="terminal-kpi-grid secondary-kpis">
        <Kpi
          label="Completed simulations"
          value={m.completed_simulations}
          note="Only known matched cashflows"
        />
        <Kpi label="Missed candidates" value={m.missed_simulations} />
        <Kpi
          label="Win rate"
          value={m.win_rate_pct}
          unit="%"
          note={`${text(m.completed_with_available_cad_pnl, "0")} completed CAD-valued samples`}
        />
        <Kpi label="Maximum drawdown" value={m.maximum_drawdown_pct} unit="%" />
      </div>
      {Number(m.completed_with_available_cad_pnl) > 0 &&
        Number(m.completed_with_available_cad_pnl) < 30 && (
          <Warning>
            Small completed-trade sample. Win rate is descriptive and does not establish expected
            profitability or statistical confidence.
          </Warning>
        )}
      <section className="terminal-panel">
        <SectionTitle eyebrow="EQUITY HISTORY" title="Portfolio and no-trading benchmark">
          <Tag value={session?.classification} />
        </SectionTitle>
        <HistoryChart points={series.data?.items || []} />
        <p className="terminal-footnote">
          Sampled recorded checkpoints · {series.data?.available_count ?? "—"} available records ·
          exact source values in chart tooltips and export.
        </p>
      </section>
      <div className="terminal-split">
        <section className="terminal-panel">
          <SectionTitle title="Simulated cumulative P&L" />
          <HistoryChart points={series.data?.items || []} kind="pnl" />
        </section>
        <section className="terminal-panel">
          <SectionTitle title="Portfolio drawdown" />
          <HistoryChart points={series.data?.items || []} kind="drawdown" />
        </section>
      </div>
      <div className="terminal-split">
        <section className="terminal-panel">
          <SectionTitle title="Simulation outcomes" />
          <AmountBars rows={outcomes} unit="attempts" empty="Zero simulation attempts" />
          <p className="terminal-footnote">
            Counts are shown directly. No percentage chart implies statistical significance.
          </p>
        </section>
        <section className="terminal-panel">
          <SectionTitle title="Fees and paid expenses" />
          <AmountBars rows={expenses} empty="No valued expense records" />
          <p className="terminal-footnote">
            Disclosures already affect cashflows/basis; never deducted again.
          </p>
          <dl className="terminal-definition-list">
            <div>
              <dt>FX conversion expenses</dt>
              <dd>
                <Exact value={expense.fx_conversion_costs_cad} unit="CAD" />
              </dd>
            </div>
            <div>
              <dt>Transfer / rebalancing paid costs</dt>
              <dd>
                <Exact value={expense.transfer_rebalancing_expenses_cad} unit="CAD" />
              </dd>
            </div>
          </dl>
          <p className="terminal-footnote">
            {text(expense.scope, "")} {text(expense.fx_costs_reason, "")}
          </p>
        </section>
      </div>
      <section className="terminal-panel">
        <SectionTitle eyebrow="PERFORMANCE RECONCILIATION" title="Financial breakdown" />
        <div className="terminal-split">
          <dl className="terminal-definition-list">
            {[
              ["Realized simulated trading P&L", p?.realized_trading_pnl_cad],
              ["Realized FX P&L", p?.realized_fx_pnl_cad],
              ["Unrealized inventory P&L", p?.unrealized_pnl_cad],
              ["Gross completed trade cashflow P&L", m.gross_completed_trade_pnl_cad],
              ["Net completed trade cashflow P&L", m.net_completed_trade_pnl_cad],
              ["Average completed trade result", m.average_completed_trade_profit_cad],
            ].map(([label, value]) => (
              <div key={String(label)}>
                <dt>{String(label)}</dt>
                <dd>
                  <Exact value={value} unit="CAD" color />
                </dd>
              </div>
            ))}
          </dl>
          <dl className="terminal-definition-list">
            {[
              ["No-trading benchmark", p?.no_trading_benchmark_cad, "CAD"],
              ["Capital utilization", p?.capital_utilization_pct, "%"],
              ["Return on mean committed capital", m.pnl_per_mean_reserved_capital_pct, "%"],
              ["Paid trading fees", p?.fees_cad, "CAD"],
              ["Paid expenses", p?.expenses_cad, "CAD"],
              [
                "Ledger reconciliation delta",
                asRecord(live.data?.reconciliation).delta_cad || p?.reconciliation_delta_cad,
                "CAD",
              ],
            ].map(([label, value, unit]) => (
              <div key={String(label)}>
                <dt>{String(label)}</dt>
                <dd>
                  <Exact value={value} unit={String(unit)} />
                </dd>
              </div>
            ))}
          </dl>
        </div>
      </section>
      <div className="terminal-split">
        <section className="terminal-panel">
          <SectionTitle title="Route performance" />
          <AmountBars rows={routes} />
        </section>
        <section className="terminal-panel">
          <SectionTitle title="Missed and failed reasons" />
          {Object.keys(asRecord(m.failure_reasons)).length ? (
            <dl className="terminal-definition-list">
              {Object.entries(asRecord(m.failure_reasons)).map(([key, value]) => (
                <div key={key}>
                  <dt>{key.replaceAll("_", " ")}</dt>
                  <dd>{text(value)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <Empty title="No recorded failure reasons">
              No reason is inferred from missing records.
            </Empty>
          )}
        </section>
      </div>
      {groups.map(([title, key]) => (
        <section className="terminal-panel" key={key}>
          <SectionTitle title={`Performance by ${title.toLowerCase()}`} />
          {key === "by_strategy" && (
            <AmountBars
              rows={asRecords(m[key]).map((x) => ({
                label: text(x.key).slice(0, 8),
                value: x.net_simulated_pnl_cad,
              }))}
              empty="No completed strategy comparison"
            />
          )}
          <GroupTable rows={asRecords(m[key])} title={title} />
        </section>
      ))}
      <section className="terminal-panel">
        <SectionTitle title="Performance by trade size" />
        <GroupTable rows={live.data?.by_trade_size || []} title="Trade sizes" />
      </section>
      <section className="terminal-export-note">
        <ShieldCheck size={20} />
        <div>
          <h3>Reconstructable reports, without AI conclusions</h3>
          <p>
            ZIP includes exact decimals, actual records, fee/FX assumptions, configuration versions,
            ledger reconciliation, opening inventory and limitations. Empty tables contain headers
            only.
          </p>
          <a
            className="text-link"
            href="/api/reports/contract"
            download="quantix-report-contract.json"
          >
            Download report contract
          </a>
        </div>
      </section>
      {(live.data?.limitations || []).map((value, i) => (
        <Warning key={i}>{value}</Warning>
      ))}
      <JsonDetails
        title="Reporting scope, definitions and reconciliation"
        value={{
          period: live.data?.period,
          reconciliation: live.data?.reconciliation,
          definitions: { period: m.period_definition, statistics: m.statistical_definition },
          sampling: series.data?.sampling,
          as_of: p?.as_of ? utcLabel(p.as_of) : null,
        }}
      />
    </div>
  );
}
