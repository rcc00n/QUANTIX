"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { CohortOpening } from "@/components/cohort-opening";
import { Empty, Exact, SectionTitle, Warning } from "@/components/terminal-ui";
import { terminalFetch } from "@/lib/terminal";
import {
  STRATEGY_LAB_ENTRY,
  STRATEGY_LAB_PROFILES,
  preferredPaperSession,
  utcLabel,
  type PaperSession,
  type PortfolioValuationStatus,
  type StrategyLabProfile,
} from "@/lib/paper";

type Checkpoint = {
  as_of: string | null;
  recorded_at: string | null;
  equity_cad: string | null;
  benchmark_cad: string | null;
  valuation_status: PortfolioValuationStatus;
  equity_reconciled: boolean | null;
};
type LabRow = {
  session_id: string;
  name: string;
  profile: StrategyLabProfile | "BASELINE";
  strategy_version_id: string | null;
  strategy_version: number;
  started_at: string;
  initial_capital_cad: string;
  opening: Checkpoint;
  closing: Checkpoint;
  valuation_status: PortfolioValuationStatus;
  completed_simulations: number;
  partially_completed: number;
  failed_simulations: number;
  missed_simulations: number;
  expired_simulations: number;
  realized_trading_pnl_cad: string | null;
  realized_fx_pnl_cad: string | null;
  fees_cad: string | null;
  expenses_cad: string | null;
  unrealized_pnl_cad: string | null;
  portfolio_return_pct: string | null;
  benchmark_return_pct: string | null;
  excess_return_pct: string | null;
  strategy_excess_cad: string | null;
  drawdown_pct: string | null;
  capital_utilization_pct: string | null;
  inventory_exposure_cad: string | null;
  largest_exchange_share_pct: string | null;
  retained_checkpoint_count: number;
  complete_checkpoint_count: number;
  ledger_reconciled: boolean | null;
  limitations: string[];
};
type LabResult = {
  schema_version: "quantix-strategy-lab-v1";
  kind: "FORWARD_PAPER_OBSERVED";
  paper_only: true;
  period: { start: string; end: string };
  initial_capital_cad: string;
  rows: LabRow[];
  limitations: string[];
};

const capitalOptions = ["10000", "50000", "100000"];
const profileNames = {
  ...STRATEGY_LAB_PROFILES,
  BASELINE: "Baseline",
} as const;

function label(value: string) {
  return value.replaceAll("_", " ").toLowerCase();
}

function ComparisonResult({ result }: { result: LabResult }) {
  return (
    <div aria-label="Recorded forward paper comparison" role="region">
      <p className="terminal-scope-note">
        Recorded forward paper results · {utcLabel(result.period.start)} to{" "}
        {utcLabel(result.period.end)} ·{" "}
        <Exact value={result.initial_capital_cad} places={2} unit="CAD" /> per portfolio
      </p>
      <div className="table-wrap">
        <table className="terminal-table" aria-label="Independent strategy portfolio comparison">
          <thead>
            <tr>
              {[
                "Portfolio and profile",
                "Completed / partial / failed / missed",
                "Realized trading · CAD",
                "Unrealized inventory · CAD",
                "Portfolio return",
                "Benchmark return",
                "Excess return",
                "Drawdown",
                "Mean capital utilization",
                "Valuation and ledger",
              ].map((name) => (
                <th key={name} scope="col">
                  {name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row) => (
              <tr key={row.session_id}>
                <th scope="row">
                  {row.name}
                  <small className="eligibility-note">
                    {profileNames[row.profile] ?? label(row.profile)}
                  </small>
                </th>
                <td>
                  {row.completed_simulations} / {row.partially_completed} / {row.failed_simulations}{" "}
                  / {row.missed_simulations}
                </td>
                <td>
                  <Exact value={row.realized_trading_pnl_cad} places={2} color />
                </td>
                <td>
                  <Exact value={row.unrealized_pnl_cad} places={2} color />
                </td>
                <td>
                  <Exact value={row.portfolio_return_pct} unit="%" color />
                </td>
                <td>
                  <Exact value={row.benchmark_return_pct} unit="%" />
                </td>
                <td>
                  <Exact value={row.excess_return_pct} unit="%" color />
                </td>
                <td>
                  <Exact value={row.drawdown_pct} unit="%" />
                </td>
                <td>
                  <Exact value={row.capital_utilization_pct} unit="%" />
                </td>
                <td>
                  {label(row.valuation_status)}
                  <small className="eligibility-note">
                    {row.ledger_reconciled === null
                      ? "Reconciliation unavailable"
                      : row.ledger_reconciled
                        ? "Ledger reconciled"
                        : "Ledger discrepancy"}
                  </small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.rows.map((row) => (
        <details className="evidence-details" key={row.session_id}>
          <summary>{row.name} · valuation and evidence</summary>
          <dl className="settings-list">
            <div>
              <dt>Portfolio ID</dt>
              <dd>{row.session_id}</dd>
            </div>
            <div>
              <dt>Strategy version</dt>
              <dd>
                Version {row.strategy_version} · {row.strategy_version_id ?? "Unavailable"}
              </dd>
            </div>
            <div>
              <dt>Portfolio opened</dt>
              <dd>{utcLabel(row.started_at)}</dd>
            </div>
            {(
              [
                ["Opening", row.opening],
                ["Closing", row.closing],
              ] as const
            ).map(([name, checkpoint]) => (
              <div key={name}>
                <dt>{name} recorded checkpoint</dt>
                <dd>
                  {utcLabel(checkpoint?.as_of)} · Equity{" "}
                  <Exact value={checkpoint?.equity_cad} places={2} unit="CAD" /> · Benchmark{" "}
                  <Exact value={checkpoint?.benchmark_cad} places={2} unit="CAD" />
                  <small className="eligibility-note">
                    {label(checkpoint.valuation_status)} · recorded{" "}
                    {utcLabel(checkpoint.recorded_at)}
                    {checkpoint.equity_reconciled === true && " · equity reconciled"}
                    {checkpoint.equity_reconciled === false &&
                      " · equity reconciliation discrepancy"}
                  </small>
                </dd>
              </div>
            ))}
            <div>
              <dt>Complete / retained valuation checkpoints</dt>
              <dd>
                {row.complete_checkpoint_count} / {row.retained_checkpoint_count}
              </dd>
            </div>
            <div>
              <dt>Expired paper attempts</dt>
              <dd>{row.expired_simulations}</dd>
            </div>
            {(
              [
                ["Trading fees", row.fees_cad, "CAD"],
                ["Other recorded expenses", row.expenses_cad, "CAD"],
                ["Realized FX", row.realized_fx_pnl_cad, "CAD"],
                ["Strategy excess at cutoff", row.strategy_excess_cad, "CAD"],
                ["Crypto inventory exposure at cutoff", row.inventory_exposure_cad, "CAD"],
                ["Largest exchange share at cutoff", row.largest_exchange_share_pct, "%"],
              ] as const
            ).map(([name, value, unit]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd>
                  <Exact value={value} places={2} unit={unit} />
                </dd>
              </div>
            ))}
          </dl>
          {!!row.limitations.length && (
            <ul className="terminal-description list-disc pl-5">
              {row.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </details>
      ))}
      {!!result.limitations.length && (
        <ul className="terminal-description list-disc pl-5">
          {result.limitations.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function StrategyLab({
  sessions,
  loading,
  sessionError,
}: {
  sessions: PaperSession[];
  loading: boolean;
  sessionError: string;
}) {
  const [capital, setCapital] = useState(capitalOptions[0]);
  const [selected, setSelected] = useState<string[] | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [result, setResult] = useState<LabResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const candidates = sessions.filter(
    (row) =>
      row.classification === "RESEARCH" &&
      row.status !== "ARCHIVED" &&
      row.initial_equity_cad.replace(/\.0+$/, "") === capital,
  );
  const opening = preferredPaperSession(candidates)?.lab_cohort;
  const peers = opening ? candidates.filter((row) => row.lab_cohort?.id === opening.id) : [];
  const common =
    peers.length === 4 &&
    Object.keys(STRATEGY_LAB_PROFILES).every((profile) =>
      peers.some((row) => row.settings.strategy_lab_profile === profile),
    )
      ? opening
      : null;
  const defaultIds = common ? peers.map((row) => row.id) : [];
  const cohort = (selected ?? defaultIds).filter((id) => candidates.some((row) => row.id === id));
  const openingInput = common?.started_at.slice(0, 19) ?? "";
  useEffect(() => {
    if (openingInput) setStart((previous) => previous || openingInput);
  }, [openingInput]);

  async function compare(event: FormEvent) {
    event.preventDefault();
    if (cohort.length < 2 || busy) return;
    setError("");
    setResult(null);
    setBusy(true);
    try {
      const from =
        common && start === openingInput ? common.started_at : new Date(`${start}Z`).toISOString();
      const to = new Date(`${end}Z`).toISOString();
      if (Date.parse(from) >= Date.parse(to))
        throw new Error("Comparison end must follow the start.");
      const comparison = await terminalFetch<LabResult>(
        "/analytics/strategy-lab/compare",
        undefined,
        {
          method: "POST",
          body: JSON.stringify({ session_ids: cohort, start: from, end: to }),
        },
      );
      setResult(comparison);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Strategy comparison unavailable");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="terminal-panel" aria-label="Strategy Lab">
      <SectionTitle title="Strategy Lab" />
      <p className="terminal-description">
        Compare independent research portfolios with equal starting capital over a shared UTC window
        of up to 31 days. Each portfolio keeps its own no-trading benchmark.
      </p>
      {common && (
        <p className="terminal-scope-note break-all">
          PAPER ONLY · RESEARCH · Experiment {common.id} · T0 {utcLabel(common.started_at)}
        </p>
      )}
      {common?.allocation && (
        <CohortOpening allocation={common.allocation} proposalId={common.proposal_id} />
      )}
      {sessionError && <Warning>{sessionError}</Warning>}
      <form onSubmit={compare}>
        <fieldset disabled={busy} className="paper-form-grid">
          <label>
            Starting capital per portfolio · CAD
            <select
              value={capital}
              onChange={(event) => {
                setCapital(event.target.value);
                setSelected(null);
                setStart("");
                setEnd("");
                setResult(null);
                setError("");
              }}
            >
              {capitalOptions.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label>
            Comparison start · UTC
            <input
              type="datetime-local"
              step={1}
              required
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </label>
          <label>
            Comparison end · UTC
            <input
              type="datetime-local"
              step={1}
              required
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </label>
        </fieldset>
        {candidates.length >= 2 ? (
          <fieldset disabled={busy} className="terminal-toggle-grid max-h-72 overflow-y-auto">
            <legend>Select two to four research portfolios</legend>
            {candidates.map((row) => {
              const checked = cohort.includes(row.id);
              const profile = row.settings.strategy_lab_profile ?? "BASELINE";
              return (
                <label key={row.id} className="terminal-toggle">
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={!checked && cohort.length >= 4}
                    onChange={(event) => {
                      setSelected((ids) =>
                        event.target.checked
                          ? [...(ids ?? defaultIds), row.id]
                          : (ids ?? defaultIds).filter((id) => id !== row.id),
                      );
                    }}
                  />
                  <span>
                    <strong>{row.name}</strong>
                    <small>
                      {profileNames[profile] ?? label(profile)} · {utcLabel(row.started_at)}
                    </small>
                    {common && row.lab_cohort?.id === common.id && (
                      <>
                        <small className="break-all">
                          {row.id} · <Exact value={row.initial_equity_cad} places={2} unit="CAD" />
                        </small>
                        {profile !== "BASELINE" && <small>{STRATEGY_LAB_ENTRY[profile]}</small>}
                      </>
                    )}
                  </span>
                </label>
              );
            })}
          </fieldset>
        ) : !loading && !sessionError ? (
          <Empty title="Two research portfolios with this capital are required">
            Open the <Link href="/portfolio">Funding Planner</Link> to review independent research
            proposals.
          </Empty>
        ) : null}
        <div className="paper-actions">
          <Button type="submit" variant="outline" disabled={busy || cohort.length < 2}>
            {busy ? "Comparing…" : "Compare strategy portfolios"}
          </Button>
        </div>
      </form>
      {error && <Warning>{error}</Warning>}
      {result && <ComparisonResult result={result} />}
    </section>
  );
}
