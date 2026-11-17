"use client";

import { useRef, useState, type FormEvent } from "react";
import { ArrowRight, Plus, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { terminalFetch, useLive } from "@/lib/terminal";
import { Exact, SectionTitle, Warning, Tag, Empty, JsonDetails } from "@/components/terminal-ui";
import { assetLabel, utcLabel, type PaperSession } from "@/lib/paper";
import { RebalancePreview } from "@/components/rebalance-preview";
import {
  AllocationBreakdown,
  FundingModeComparison,
  FundingRouteDetails,
  POLICY_LABELS,
  type FundingCatalog,
  type FundingCoverage,
  type FundingPolicy,
  type FundingProposal,
} from "@/components/funding-details";

const limits = [
  { key: "min_buy_capital_cad", label: "Minimum buy capital · CAD", value: "50" },
  { key: "min_cell_cad", label: "Minimum position · CAD", value: "50" },
  { key: "max_venue_pct", label: "Maximum per exchange · %", value: "60" },
  { key: "max_asset_pct", label: "Maximum per asset · %", value: "70" },
  { key: "max_reserve_pct", label: "Maximum reserve · %", value: "50" },
  { key: "max_stablecoin_pct", label: "Maximum stablecoin exposure · %", value: "50" },
] as const;

type LimitValues = Record<(typeof limits)[number]["key"], string>;
const initialLimits = Object.fromEntries(
  limits.map(({ key, value }) => [key, value]),
) as LimitValues;

type PlanAttempt = {
  name: string;
  proposal_id: string;
  capital_cad: string;
  idempotency_key: string;
  reset_from_session_id?: string;
};

function splitFilter(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function FundingPlanner({
  session,
  onCreated,
  initialCapital,
  initialName,
}: {
  session: PaperSession | null;
  onCreated?: () => Promise<void>;
  initialCapital?: string;
  initialName?: string;
}) {
  const [inventoryOpen, setInventoryOpen] = useState(false);
  const catalog = useLive<FundingCatalog>("/paper/funding/catalog", 60000);
  const coverage = useLive<FundingCoverage>(
    inventoryOpen && session ? `/paper/funding/coverage?session_id=${session.id}&limit=50` : null,
    30000,
  );
  const [capital, setCapital] = useState(initialCapital ?? session?.initial_equity_cad ?? "10000");
  const [tradeBudget, setTradeBudget] = useState("250");
  const [policy, setPolicy] = useState<FundingPolicy>("DIVERSIFIED_HYBRID");
  const [eligibility, setEligibility] = useState<"RESEARCH" | "VALIDATED">("RESEARCH");
  const [exchanges, setExchanges] = useState("");
  const [assets, setAssets] = useState("");
  const [inventoryAssets, setInventoryAssets] = useState("");
  const [reserve, setReserve] = useState("10");
  const [constraints, setConstraints] = useState<LimitValues>(initialLimits);
  const [minVenues, setMinVenues] = useState("2");
  const [lookback, setLookback] = useState("7");
  const [cutoff, setCutoff] = useState("");
  const [name, setName] = useState(initialName ?? "Prefunded research experiment");
  const [plans, setPlans] = useState<FundingProposal[]>([]);
  const [chosen, setChosen] = useState<string | null>(null);
  const [comparisonAsOf, setComparisonAsOf] = useState<string>();
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState<"proposal" | "compare" | "creation" | null>(null);
  const [created, setCreated] = useState<Record<string, string>>({});
  const scenario = useRef("");
  const attempts = useRef(new Map<string, PlanAttempt>());
  const inFlight = useRef(false);
  const proposal = plans.find((plan) => plan.id === chosen);
  const pending = proposal ? attempts.current.get(proposal.id) : undefined;

  async function propose(event: FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    const compare =
      (event.nativeEvent as SubmitEvent).submitter?.getAttribute("value") === "compare";
    const common = {
      capital_cad: capital,
      trade_amount_cad: tradeBudget,
      exchange_slugs: splitFilter(exchanges),
      asset_keys: splitFilter(assets),
      inventory_asset_keys: splitFilter(inventoryAssets),
      reserve_cash_pct: reserve,
      lookback_days: Number(lookback),
      ...(cutoff.trim() ? { planning_cutoff: cutoff.trim() } : {}),
      eligibility,
      ...constraints,
      min_venues: Number(minVenues),
    };
    const signature = JSON.stringify(common);
    inFlight.current = true;
    setError("");
    setMessage("");
    setBusy(compare ? "compare" : "proposal");
    try {
      if (compare) {
        const batch = await terminalFetch<{ as_of: string; proposals: FundingProposal[] }>(
          "/paper/funding/compare",
          undefined,
          { method: "POST", body: JSON.stringify(common) },
        );
        scenario.current = signature;
        setComparisonAsOf(batch.as_of);
        setPlans(batch.proposals);
        setChosen(null);
        return;
      }
      const value = await terminalFetch<FundingProposal>("/paper/funding/proposal", undefined, {
        method: "POST",
        body: JSON.stringify({ ...common, policy }),
      });
      const sameScenario = signature === scenario.current;
      scenario.current = signature;
      setComparisonAsOf(undefined);
      setPlans((old) =>
        [value, ...(sameScenario ? old.filter((plan) => plan.policy !== value.policy) : [])].slice(
          0,
          5,
        ),
      );
      setChosen(value.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Funding proposal unavailable");
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  async function create(reset = false) {
    if (
      !proposal ||
      proposal.feasible === false ||
      proposal.creatable === false ||
      proposal.eligibility === "VALIDATED" ||
      inFlight.current
    )
      return;
    const attempt = attempts.current.get(proposal.id) ?? {
      name: name.trim(),
      proposal_id: proposal.id,
      capital_cad: proposal.capital_cad,
      idempotency_key: `funding:${proposal.id}`,
      ...(reset && session ? { reset_from_session_id: session.id } : {}),
    };
    attempts.current.set(proposal.id, attempt);
    inFlight.current = true;
    setError("");
    setMessage("");
    setBusy("creation");
    try {
      const result = await terminalFetch<{ id: string; reused?: boolean }>(
        "/paper/funding/experiments",
        undefined,
        { method: "POST", body: JSON.stringify(attempt) },
      );
      setCreated((old) => ({ ...old, [proposal.id]: result.id }));
      setMessage(
        `${result.reused ? "Existing" : "Created independent"} research experiment ${result.id.slice(0, 8)}.`,
      );
      await onCreated?.();
      coverage.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Experiment could not be created");
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  return (
    <section
      id="virtual-funding-planner"
      className="terminal-panel funding-planner"
      aria-label="Virtual Funding Planner"
    >
      <SectionTitle title="Virtual Funding Planner">
        <Tag value="PAPER ONLY" />
      </SectionTitle>
      <p className="terminal-description">
        Compare how the same starting capital could fund both sides of observed routes.
      </p>
      {(error || coverage.error) && <Warning>{error || coverage.error}</Warning>}
      {message && (
        <div className="terminal-success" role="status">
          {message}
        </div>
      )}
      <form onSubmit={propose} className="funding-form">
        <div className="terminal-filterbar">
          <label>
            Starting capital · CAD
            <input
              aria-label="Funding capital CAD"
              inputMode="decimal"
              value={capital}
              onChange={(e) => setCapital(e.target.value)}
              required
            />
          </label>
          <label>
            Target trade size · CAD
            <input
              aria-label="Funding target trade budget CAD"
              inputMode="decimal"
              value={tradeBudget}
              onChange={(e) => setTradeBudget(e.target.value)}
              required
              list="funding-sizes"
            />
            <datalist id="funding-sizes">
              {["50", "100", "250", "500", "1000", "2500", "5000"].map((size) => (
                <option key={size} value={size} />
              ))}
            </datalist>
          </label>
          <label>
            Allocation policy
            <select
              aria-label="Funding allocation policy"
              value={policy}
              onChange={(e) => setPolicy(e.target.value as FundingPolicy)}
            >
              {Object.entries(POLICY_LABELS).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Eligible route evidence
            <select
              aria-label="Funding eligibility"
              value={eligibility}
              onChange={(e) => setEligibility(e.target.value as typeof eligibility)}
            >
              <option value="RESEARCH">Research scope</option>
              <option value="VALIDATED">Verified evidence only</option>
            </select>
          </label>
        </div>
        <details className="planner-inventory funding-constraints">
          <summary>Limits, reserve and allowed markets</summary>
          <div className="terminal-filterbar">
            <label>
              Target cash reserve · %
              <input
                aria-label="Funding reserve percentage"
                inputMode="decimal"
                value={reserve}
                onChange={(e) => setReserve(e.target.value)}
                required
              />
            </label>
            {limits.map(({ key, label }) => (
              <label key={key}>
                {label}
                <input
                  inputMode="decimal"
                  value={constraints[key]}
                  onChange={(e) => setConstraints((old) => ({ ...old, [key]: e.target.value }))}
                  required
                />
              </label>
            ))}
            <label>
              Minimum funded exchanges
              <input
                type="number"
                min={1}
                step={1}
                value={minVenues}
                onChange={(e) => setMinVenues(e.target.value)}
                required
              />
            </label>
            <label>
              Historical lookback · days
              <input
                type="number"
                min={1}
                max={30}
                step={1}
                value={lookback}
                onChange={(e) => setLookback(e.target.value)}
                required
              />
            </label>
            <label>
              Exchange slugs · optional
              <input
                aria-label="Funding exchanges"
                value={exchanges}
                onChange={(e) => setExchanges(e.target.value)}
                placeholder="kraken, ndax"
                list="funding-venues"
              />
              <datalist id="funding-venues">
                {catalog.data?.exchanges.map((x) => (
                  <option key={x.id} value={x.slug}>
                    {x.name}
                  </option>
                ))}
              </datalist>
            </label>
            <label>
              Route base assets · optional
              <input
                aria-label="Funding assets"
                value={assets}
                onChange={(e) => setAssets(e.target.value)}
                placeholder="asset:BTC, fiat:CAD"
                list="funding-assets"
              />
              <datalist id="funding-assets">
                {catalog.data?.assets.map((x) => (
                  <option key={x.id} value={x.canonical_key}>
                    {x.symbol}
                  </option>
                ))}
              </datalist>
            </label>
            <label>
              Allowed inventory assets · optional
              <input
                value={inventoryAssets}
                onChange={(e) => setInventoryAssets(e.target.value)}
                placeholder="asset:BTC, fiat:CAD"
                list="funding-assets"
              />
            </label>
            <label>
              Historical cutoff · UTC ISO · optional
              <input
                value={cutoff}
                onChange={(e) => setCutoff(e.target.value)}
                placeholder="2026-11-22T00:00:00Z"
                autoComplete="off"
              />
            </label>
          </div>
          <p className="terminal-footnote">
            Legacy policies keep their original allocation rules for comparison.
          </p>
        </details>
        <div className="funding-actions">
          <Button type="submit" disabled={busy !== null}>
            {busy === "proposal" ? "Calculating…" : "Propose virtual funding"}{" "}
            <ArrowRight size={14} />
          </Button>
          <Button type="submit" variant="outline" value="compare" disabled={busy !== null}>
            {busy === "compare" ? "Comparing…" : "Compare all modes"}
          </Button>
          {busy !== null && (
            <span className="terminal-footnote" role="status">
              {busy === "creation"
                ? "Starting paper portfolio…"
                : "Checking retained routes and inventory requirements…"}
            </span>
          )}
        </div>
      </form>
      {plans.length > 0 && (
        <FundingModeComparison
          plans={plans}
          chosen={chosen}
          setChosen={setChosen}
          comparisonAsOf={comparisonAsOf}
        />
      )}
      {plans.length > 0 && !proposal && (
        <p className="terminal-footnote">
          Select a mode to inspect its allocation and use its proposal.
        </p>
      )}
      {proposal && (
        <section className="funding-selected" aria-label="Selected funding proposal">
          <div className="section-heading funding-result-heading">
            <div>
              <span className="small-label">SELECTED PROPOSAL</span>
              <h3>{POLICY_LABELS[proposal.policy] ?? proposal.policy.replaceAll("_", " ")}</h3>
            </div>
            {proposal.feasible !== undefined && (
              <Tag value={proposal.feasible ? "WITHIN CONSTRAINTS" : "INFEASIBLE"} />
            )}
            {proposal.creatable === false && proposal.feasible !== false && (
              <Tag value="STUDY ONLY" />
            )}
          </div>
          <AllocationBreakdown proposal={proposal} catalog={catalog.data} />
          {proposal.reasons?.length ? (
            <Warning>
              {proposal.reasons.map((reason) => reason.replaceAll("_", " ")).join(" · ")}
            </Warning>
          ) : null}
          {proposal.limitations.map((limitation) => (
            <p className="terminal-footnote" key={limitation}>
              {limitation}
            </p>
          ))}
          <JsonDetails
            title="Inspect retained allocation evidence"
            value={{
              coverage: proposal.route_coverage,
              requirements: proposal.requirements,
              evidence: proposal.evidence,
              provenance: proposal.provenance,
            }}
          />
          <div className="funding-create">
            <div>
              <h3>Start an independent research experiment</h3>
              <p className="terminal-footnote">
                This starts a new paper portfolio from the selected proposal.
              </p>
            </div>
            {created[proposal.id] ? (
              <p className="terminal-footnote" role="status">
                Portfolio created from this proposal.
              </p>
            ) : proposal.creatable === false && proposal.feasible !== false ? (
              <p className="terminal-footnote">
                This study proposal cannot fund a new portfolio. Request a fresh research proposal
                with current funding marks.
              </p>
            ) : proposal.feasible === false || proposal.eligibility === "VALIDATED" ? (
              <p className="terminal-footnote">
                Revise the constraints or select an eligible research proposal to create a
                portfolio.
              </p>
            ) : (
              <div className="terminal-filterbar">
                <label>
                  Research experiment name
                  <input
                    aria-label="Research experiment name"
                    value={pending?.name ?? name}
                    onChange={(e) => setName(e.target.value)}
                    readOnly={Boolean(pending)}
                    required
                    maxLength={100}
                  />
                </label>
                <Button
                  disabled={
                    busy !== null || !name.trim() || Boolean(pending?.reset_from_session_id)
                  }
                  onClick={() => void create()}
                >
                  <Plus size={14} />{" "}
                  {pending && busy !== "creation" && !pending.reset_from_session_id
                    ? "Retry experiment creation"
                    : "Create independent experiment"}
                </Button>
                {session?.classification === "RESEARCH" && (
                  <Button
                    variant="outline"
                    disabled={
                      busy !== null ||
                      !name.trim() ||
                      Boolean(pending && !pending.reset_from_session_id)
                    }
                    onClick={() => void create(true)}
                  >
                    <RefreshCw size={14} />{" "}
                    {pending?.reset_from_session_id
                      ? "Retry new experiment"
                      : "Reset as new experiment"}
                  </Button>
                )}
              </div>
            )}
          </div>
        </section>
      )}
      {session && (
        <details
          className="planner-inventory"
          onToggle={(event) => setInventoryOpen(event.currentTarget.open)}
        >
          <summary>Current virtual funds and route constraints</summary>
          {coverage.loading && (
            <p className="terminal-footnote" role="status">
              Loading available and reserved funds…
            </p>
          )}
          <div className="table-wrap">
            <table className="terminal-table" aria-label="Funding inventory">
              <thead>
                <tr>
                  <th>Venue</th>
                  <th>Asset</th>
                  <th>Available</th>
                  <th>Reserved</th>
                </tr>
              </thead>
              <tbody>
                {coverage.data?.balances.map((row) => (
                  <tr key={`${row.exchange}:${row.canonical_key || row.asset}`}>
                    <td>{row.exchange}</td>
                    <td>{row.asset || assetLabel(row.canonical_key || "")}</td>
                    <td>
                      <Exact value={row.available_quantity} places={8} />
                    </td>
                    <td>
                      <Exact value={row.reserved_quantity} places={8} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {coverage.data?.routes.length ? (
            <>
              {coverage.data.session_trade_cap_cad && (
                <p className="terminal-footnote">
                  Session trade cap <Exact value={coverage.data.session_trade_cap_cad} unit="CAD" />
                  {" · "}Minimum buy capital{" "}
                  <Exact value={coverage.data.session_minimum_buy_capital_cad} unit="CAD" />
                </p>
              )}
              <FundingRouteDetails
                key={coverage.data.session_id ?? session.id}
                routes={coverage.data.routes}
                basis="Capped funded size"
              />
            </>
          ) : !coverage.loading ? (
            <Empty title="No retained routes to assess">
              Coverage requires retained compatible opportunity evidence.
            </Empty>
          ) : null}
          {coverage.data?.window?.limited && (
            <p className="terminal-footnote">
              Assessment is bounded to {coverage.data.window.limit} retained candidates.
            </p>
          )}
          {coverage.data?.limitations.map((limitation) => (
            <p className="terminal-footnote" key={limitation}>
              {limitation}
            </p>
          ))}
        </details>
      )}
      {session?.classification === "RESEARCH" && (
        <RebalancePreview sessionId={session.id} catalog={catalog.data} />
      )}
    </section>
  );
}
