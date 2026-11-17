"use client";

import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Exact, Tag } from "@/components/terminal-ui";
import { assetLabel, utcLabel } from "@/lib/paper";

export const POLICY_LABELS = {
  COVERAGE: "Baseline route coverage",
  LIQUIDITY_WEIGHTED: "Liquidity weighted",
  ROUTE_OPTIMIZED: "Route optimized",
  DIVERSIFIED_HYBRID: "Diversified hybrid",
  EQUAL_VENUES: "Legacy equal venues",
} as const;
export type FundingPolicy = keyof typeof POLICY_LABELS;
export type FundingCatalog = {
  exchanges: { id: string; slug: string; name: string }[];
  assets: { id: string; canonical_key: string; symbol: string; kind: string }[];
};
type Position = {
  exchange: string;
  asset?: string;
  canonical_key?: string;
  available_quantity?: string | null;
  reserved_quantity?: string | null;
};
type FundingRequirement = Position & {
  side?: "BUY" | "SELL" | "buy" | "sell";
  required_quantity?: string | null;
  missing_quantity?: string | null;
  fee_asset?: string;
  total_quantity?: string | null;
};
type ProjectedCosts = {
  fees_cad: string | null;
  fx_drag_cad: string | null;
  slippage_reserve_cad: string | null;
  rebalancing_cad: string | null;
};
export type FundingRoute = {
  route_id: string;
  asset?: string;
  buy_exchange?: string;
  sell_exchange?: string;
  inventory_sufficient?: boolean;
  funded?: boolean;
  required: FundingRequirement[];
  reasons?: string[];
  limiting_constraints?: string[];
  actionable?: boolean;
  projected_net_profit_cad?: string | null;
  maximum_funded_acquisition_cad?: string | null;
  maximum_observed_acquisition_cad?: string | null;
  maximum_affordable_acquisition_cad?: string | null;
  funded_size_net_profit_cad?: string | null;
  projected_costs?: ProjectedCosts | null;
  market_profitability?: "PROJECTED_POSITIVE" | "NON_POSITIVE" | "UNKNOWN";
  fundable_with_reallocation?: boolean | null;
  funding_model?: "PRE_FUNDED" | "TRANSFER";
  required_inventory_mark_cad?: string | null;
  inventory_shortfall_mark_cad?: string | null;
  funded_inventory_mark_cad?: string | null;
  prefunded_expected_return_pct?: string | null;
};
export type FundingCoverage = {
  session_id?: string;
  balances: Position[];
  routes: FundingRoute[];
  limitations: string[];
  window?: { limited?: boolean; limit: number };
  session_trade_cap_cad?: string;
  session_minimum_buy_capital_cad?: string;
};
type Concentration = Record<string, { cad_value: string; share_pct: string }>;
type SizeCoverage = {
  trade_amount_cad: string;
  funded_alternative_routes: number;
  simultaneous_funded_routes: number;
  profitable_funded_routes: number;
  considered_routes: number;
};
export type FundingProposal = {
  id: string;
  capital_cad: string;
  policy: FundingPolicy;
  eligibility?: "RESEARCH" | "VALIDATED";
  feasible?: boolean;
  creatable?: boolean;
  reasons?: string[];
  allocations: {
    exchange: string;
    asset: string;
    quantity: string;
    cad_rate: string;
    cad_value: string;
    evidence?: { reserve?: boolean };
  }[];
  total_cad: string;
  unallocated_cad: string;
  trade_amount_cad?: string;
  as_of?: string;
  expires_at: string;
  allocation_version?: string;
  planning_window?: { from: string; cutoff: string; historical: boolean };
  evidence?: unknown[];
  evidence_exclusions?: Record<string, number>;
  route_coverage: {
    considered_routes?: number;
    funded_route_count?: number;
    historical_window_days?: number;
    route_window_limited?: boolean;
  };
  funded_route_count?: number;
  simultaneous_funded_route_count?: number;
  median_funded_acquisition_cad?: string | null;
  allocation_summary?: {
    allocated_cad: string;
    invested_cad: string;
    reserve_cad: string;
    reserve_pct: string;
    by_exchange: Concentration;
    by_asset: Concentration;
    stablecoin_cad: string;
    stablecoin_pct: string;
    funded_venues: number;
    funded_cells: number;
  };
  coverage_by_size?: SizeCoverage[];
  requirements?: FundingRoute[];
  route_capacity?: {
    route_id: string;
    maximum_funded_acquisition_cad: string | null;
    maximum_observed_acquisition_cad: string | null;
    projected_net_profit_cad: string | null;
    target_pinned: {
      acquisition_cad: string;
      net_profit_cad: string | null;
      projected_costs?: ProjectedCosts | null;
      deficits: {
        exchange: string;
        asset: string;
        required_quantity: string;
        available_quantity: string;
        deficit_quantity: string;
      }[];
    } | null;
    limiting_constraints: string[];
    fundable_with_reallocation: boolean;
    actionable: false;
  }[];
  provenance?: unknown;
  limitations: string[];
};

function policyLabel(policy: FundingPolicy) {
  return POLICY_LABELS[policy] ?? policy.replaceAll("_", " ");
}

function count(value: number | undefined) {
  return value ?? "Unavailable";
}

export function FundingModeComparison({
  plans,
  chosen,
  setChosen,
  comparisonAsOf,
}: {
  plans: FundingProposal[];
  chosen: string | null;
  setChosen: (id: string) => void;
  comparisonAsOf?: string;
}) {
  return (
    <section className="funding-comparisons" aria-label="Equal-capital funding proposals">
      <h3>Compare allocation modes</h3>
      <p className="terminal-footnote">
        {comparisonAsOf
          ? `Shared evidence cutoff ${utcLabel(comparisonAsOf)}.`
          : "These proposals use equal settings. Check each evidence time for separately generated plans."}{" "}
        Funding counts describe hypothetical inventory coverage.
      </p>
      <div className="table-wrap" role="region" aria-label="Scrollable comparison" tabIndex={0}>
        <table className="terminal-table" aria-label="Funding mode comparison">
          <thead>
            <tr>
              <th>Mode</th>
              <th>Capital</th>
              <th>Cash reserve</th>
              <th>Alternatives funded</th>
              <th>Funded together</th>
              <th>Plan status</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((plan) => (
              <tr key={plan.id} className={plan.id === chosen ? "funding-comparison-selected" : ""}>
                <td>
                  <button
                    className="funding-select"
                    type="button"
                    aria-pressed={plan.id === chosen}
                    onClick={() => setChosen(plan.id)}
                  >
                    {policyLabel(plan.policy)}
                  </button>
                </td>
                <td>
                  <Exact value={plan.capital_cad} unit="CAD" />
                </td>
                <td>
                  <Exact value={plan.allocation_summary?.reserve_cad} unit="CAD" />
                </td>
                <td>
                  {count(plan.funded_route_count ?? plan.route_coverage.funded_route_count)} /{" "}
                  {count(plan.route_coverage.considered_routes)}
                </td>
                <td>{count(plan.simultaneous_funded_route_count)}</td>
                <td>
                  {plan.feasible === false
                    ? "Infeasible"
                    : plan.creatable === false
                      ? "Study only"
                      : plan.feasible
                        ? "Within limits"
                        : "Legacy rules"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ConcentrationTable({
  rows,
  resolve,
  label,
}: {
  rows: Concentration;
  resolve: (key: string) => string;
  label: string;
}) {
  return (
    <div
      className="table-wrap"
      role="region"
      aria-label={`Scrollable ${label.toLowerCase()}`}
      tabIndex={0}
    >
      <table className="terminal-table" aria-label={label}>
        <thead>
          <tr>
            <th>{label}</th>
            <th>Initial CAD</th>
            <th>Capital share</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(rows).map(([key, item]) => (
            <tr key={key}>
              <td>{resolve(key)}</td>
              <td>
                <Exact value={item.cad_value} unit="CAD" />
              </td>
              <td>
                <Exact value={item.share_pct} unit="%" places={2} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CoverageTable({ rows }: { rows: SizeCoverage[] }) {
  return (
    <div className="table-wrap" role="region" aria-label="Scrollable trade sizes" tabIndex={0}>
      <table className="terminal-table" aria-label="Target trade sizes">
        <thead>
          <tr>
            <th>Buy target</th>
            <th>Funded alternatives</th>
            <th>Funded together</th>
            <th>Positive funded projections</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.trade_amount_cad}>
              <td>
                <Exact value={row.trade_amount_cad} unit="CAD" />
              </td>
              <td>
                {row.funded_alternative_routes} / {row.considered_routes}
              </td>
              <td>{row.simultaneous_funded_routes}</td>
              <td>{row.profitable_funded_routes}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AllocationBreakdown({
  proposal,
  catalog,
}: {
  proposal: FundingProposal;
  catalog: FundingCatalog | null;
}) {
  const summary = proposal.allocation_summary;
  const resolveVenue = (key: string) =>
    catalog?.exchanges.find((venue) => venue.id === key || venue.slug === key)?.name ?? key;
  const resolveAsset = (key: string) =>
    catalog?.assets.find((asset) => asset.id === key || asset.canonical_key === key)?.symbol ??
    assetLabel(key);
  const capacity = new Map(proposal.route_capacity?.map((route) => [route.route_id, route]));
  const routes = proposal.requirements?.map((route) => {
    const value = capacity.get(route.route_id);
    return value
      ? {
          ...route,
          required:
            value.target_pinned?.deficits?.map((leg, i) => ({
              ...leg,
              side: i === 0 ? ("BUY" as const) : ("SELL" as const),
              asset: resolveAsset(leg.asset),
              canonical_key: leg.asset,
              missing_quantity: leg.deficit_quantity,
            })) ?? route.required,
          maximum_funded_acquisition_cad: value.maximum_funded_acquisition_cad,
          maximum_observed_acquisition_cad: value.maximum_observed_acquisition_cad,
          funded_size_net_profit_cad: value.projected_net_profit_cad,
          projected_net_profit_cad: value.target_pinned
            ? value.target_pinned.net_profit_cad
            : route.projected_net_profit_cad,
          projected_costs: value.target_pinned
            ? value.target_pinned.projected_costs
            : route.projected_costs,
          market_profitability:
            value.target_pinned?.net_profit_cad == null
              ? ("UNKNOWN" as const)
              : value.limiting_constraints.includes("NON_POSITIVE_NET_PROJECTION")
                ? ("NON_POSITIVE" as const)
                : ("PROJECTED_POSITIVE" as const),
          limiting_constraints: value.limiting_constraints,
          fundable_with_reallocation: value.fundable_with_reallocation,
          actionable: value.actionable,
        }
      : route;
  });
  return (
    <>
      <dl className="funding-totals">
        <div>
          <dt>Starting capital</dt>
          <dd>
            <Exact value={proposal.capital_cad} unit="CAD" />
          </dd>
        </div>
        <div>
          <dt>Allocated</dt>
          <dd>
            <Exact value={summary?.allocated_cad ?? proposal.total_cad} unit="CAD" />
          </dd>
        </div>
        {summary && (
          <>
            <div>
              <dt>Committed to funding</dt>
              <dd>
                <Exact value={summary.invested_cad} unit="CAD" />
              </dd>
            </div>
            <div>
              <dt>Cash reserve</dt>
              <dd>
                <Exact value={summary.reserve_cad} unit="CAD" /> ·{" "}
                <Exact value={summary.reserve_pct} unit="%" places={2} />
              </dd>
            </div>
            <div>
              <dt>Stablecoin exposure</dt>
              <dd>
                <Exact value={summary.stablecoin_cad} unit="CAD" /> ·{" "}
                <Exact value={summary.stablecoin_pct} unit="%" places={2} />
              </dd>
            </div>
          </>
        )}
        <div>
          <dt>Unassigned capital</dt>
          <dd>
            <Exact value={proposal.unallocated_cad} unit="CAD" />
          </dd>
        </div>
      </dl>
      <p className="terminal-footnote funding-evidence-time">
        Target <Exact value={proposal.trade_amount_cad} unit="CAD" />
        {proposal.as_of ? ` · Evidence ${utcLabel(proposal.as_of)}` : ""}
        {` · Expires ${utcLabel(proposal.expires_at)}`}
      </p>
      {proposal.planning_window && (
        <details className="planner-inventory">
          <summary>Planning window and excluded evidence</summary>
          <p className="terminal-footnote">
            {proposal.planning_window.historical ? "Historical study" : "Current proposal"} ·{" "}
            {utcLabel(proposal.planning_window.from)} to {utcLabel(proposal.planning_window.cutoff)}
            {proposal.allocation_version ? ` · ${proposal.allocation_version}` : ""}
          </p>
          {proposal.evidence && (
            <p className="terminal-footnote">
              {proposal.evidence.length} retained evidence records.
            </p>
          )}
          {proposal.evidence_exclusions && (
            <ul className="funding-route-limits">
              {Object.entries(proposal.evidence_exclusions).map(([reason, count]) => (
                <li key={reason}>
                  {reason.replaceAll("_", " ")} · {count}
                </li>
              ))}
            </ul>
          )}
        </details>
      )}
      {proposal.coverage_by_size?.length ? (
        <section className="funding-group" aria-label="Both-side funding by trade size">
          <h3>Both-side funding by trade size</h3>
          <p className="terminal-footnote">
            Alternative routes may share balances. The funded-together count uses distinct
            reservations from the same inventory.
          </p>
          <CoverageTable rows={proposal.coverage_by_size} />
          <p className="terminal-footnote">
            Median funded acquisition{" "}
            <Exact value={proposal.median_funded_acquisition_cad} unit="CAD" />.
          </p>
        </section>
      ) : null}
      <section className="funding-group" aria-label="Initial allocation breakdown">
        <h3>Initial positions</h3>
        <div
          className="table-wrap"
          role="region"
          aria-label="Scrollable proposed allocations"
          tabIndex={0}
        >
          <table className="terminal-table" aria-label="Proposed funding allocations">
            <thead>
              <tr>
                <th>Venue</th>
                <th>Asset</th>
                <th>Initial quantity</th>
                <th>CAD per unit</th>
                <th>Initial CAD basis</th>
              </tr>
            </thead>
            <tbody>
              {proposal.allocations.map((a, i) => (
                <tr key={`${a.exchange}:${a.asset}:${i}`}>
                  <td>{a.exchange}</td>
                  <td>
                    {resolveAsset(a.asset)} {a.evidence?.reserve && <small>· reserve</small>}
                  </td>
                  <td>
                    <Exact value={a.quantity} places={8} />
                  </td>
                  <td>
                    <Exact value={a.cad_rate} places={8} />
                  </td>
                  <td>
                    <Exact value={a.cad_value} unit="CAD" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {summary && (
        <details className="planner-inventory">
          <summary>Capital concentration by exchange and asset</summary>
          <div className="funding-concentration">
            <ConcentrationTable
              rows={summary.by_exchange}
              resolve={resolveVenue}
              label="Exchange concentration"
            />
            <ConcentrationTable
              rows={summary.by_asset}
              resolve={resolveAsset}
              label="Asset concentration"
            />
          </div>
        </details>
      )}
      {routes?.length ? (
        <details className="planner-inventory">
          <summary>Both-leg requirements and funding constraints</summary>
          <FundingRouteDetails key={proposal.id} routes={routes} basis="Pinned market size" />
        </details>
      ) : null}
    </>
  );
}

const profitabilityLabel = {
  PROJECTED_POSITIVE: "Positive market projection",
  NON_POSITIVE: "Non-positive market projection",
  UNKNOWN: "Market profitability unavailable",
};
const routeReasons = (route: FundingRoute) => [
  ...new Set([...(route.reasons ?? []), ...(route.limiting_constraints ?? [])]),
];

function fundingLabel(route: FundingRoute, reasons: string[]) {
  if (reasons.includes("AFFORDABLE_SIZE_BELOW_MINIMUM_BUY_CAPITAL"))
    return "BELOW MINIMUM BUY CAPITAL";
  if (route.funding_model === "TRANSFER" && route.inventory_sufficient)
    return "BUY INVENTORY AVAILABLE";
  if ((route.inventory_sufficient ?? route.funded) === true) return "FUNDED";
  if (!route.required?.length) return "FUNDING UNAVAILABLE";
  if (
    reasons.includes("TARGET_ACQUISITION_NOT_OBSERVED") ||
    reasons.includes("BELOW_MINIMUM_BUY_CAPITAL")
  )
    return "TARGET SIZE UNAVAILABLE";
  return "MISSING INVENTORY";
}

export function FundingRouteDetails({
  routes,
  basis,
}: {
  routes: FundingRoute[];
  basis: "Pinned market size" | "Capped funded size";
}) {
  const [visible, setVisible] = useState(12);
  return (
    <div className="funding-routes">
      <p className="terminal-footnote">
        Funding coverage and market profitability are separate. Actionability still requires
        verified evidence and risk approval.
      </p>
      {routes.slice(0, visible).map((route) => {
        const reasons = routeReasons(route);
        return (
          <details className="funding-route" key={route.route_id}>
            <summary className="funding-route-title">
              <strong>
                {route.asset ?? "Market"} · {route.buy_exchange ?? "Buy"}{" "}
                <ArrowRight size={12} aria-hidden="true" /> {route.sell_exchange ?? "Sell"}
              </strong>
              <span className="funding-route-tags">
                <Tag value={fundingLabel(route, reasons)} />
                {route.market_profitability && (
                  <span>{profitabilityLabel[route.market_profitability]}</span>
                )}
              </span>
            </summary>
            <dl className="funding-route-metrics">
              <div>
                <dt>Maximum funded acquisition</dt>
                <dd>
                  <Exact value={route.maximum_funded_acquisition_cad} unit="CAD" />
                </dd>
              </div>
              {route.maximum_observed_acquisition_cad !== undefined && (
                <div>
                  <dt>
                    {route.funding_model
                      ? "Market capacity at session cap"
                      : "Maximum observed acquisition"}
                  </dt>
                  <dd>
                    <Exact value={route.maximum_observed_acquisition_cad} unit="CAD" />
                  </dd>
                </div>
              )}
              {route.maximum_affordable_acquisition_cad !== undefined && (
                <div>
                  <dt>Maximum affordable acquisition</dt>
                  <dd>
                    <Exact value={route.maximum_affordable_acquisition_cad} unit="CAD" />
                  </dd>
                </div>
              )}
              <div>
                <dt>Net projection at funded size</dt>
                <dd>
                  <Exact value={route.funded_size_net_profit_cad} unit="CAD" color />
                </dd>
              </div>
              <div>
                <dt>Pinned market net projection</dt>
                <dd>
                  <Exact value={route.projected_net_profit_cad} unit="CAD" color />
                </dd>
              </div>
            </dl>
            {route.funding_model === "PRE_FUNDED" && (
              <dl className="funding-route-metrics">
                <div>
                  <dt>Required two-sided inventory mark</dt>
                  <dd>
                    <Exact value={route.required_inventory_mark_cad} unit="CAD" />
                  </dd>
                </div>
                <div>
                  <dt>Shortfall at pinned liquidation marks</dt>
                  <dd>
                    <Exact value={route.inventory_shortfall_mark_cad} unit="CAD" />
                  </dd>
                </div>
                <div>
                  <dt>Funded two-sided inventory mark</dt>
                  <dd>
                    <Exact value={route.funded_inventory_mark_cad} unit="CAD" />
                  </dd>
                </div>
                <div>
                  <dt>Projected return on funded inventory</dt>
                  <dd>
                    <Exact value={route.prefunded_expected_return_pct} unit="%" />
                  </dd>
                </div>
              </dl>
            )}
            {route.projected_costs && (
              <div role="group" aria-label="Expected route costs in CAD">
                <p className="terminal-footnote">Expected costs · {basis}</p>
                <dl className="funding-route-metrics">
                  <div>
                    <dt>Taker fees</dt>
                    <dd>
                      <Exact value={route.projected_costs.fees_cad} unit="CAD" />
                    </dd>
                  </div>
                  <div>
                    <dt>Slippage reserve</dt>
                    <dd>
                      <Exact value={route.projected_costs.slippage_reserve_cad} unit="CAD" />
                    </dd>
                  </div>
                  <div>
                    <dt>FX drag (reflected in conversion)</dt>
                    <dd>
                      <Exact value={route.projected_costs.fx_drag_cad} unit="CAD" />
                    </dd>
                  </div>
                  <div>
                    <dt>Rebalancing cost</dt>
                    <dd>
                      <Exact value={route.projected_costs.rebalancing_cad} unit="CAD" />
                    </dd>
                  </div>
                </dl>
              </div>
            )}
            {(route.required ?? []).map((r, i) => (
              <div className="funding-requirement" key={`${r.exchange}:${r.asset}:${i}`}>
                <span>
                  {r.side?.toUpperCase() || (r.exchange === route.sell_exchange ? "SELL" : "BUY")} ·{" "}
                  {r.exchange} · {r.asset || assetLabel(r.canonical_key || "")}
                </span>
                <span>
                  Required <Exact value={r.required_quantity} places={8} />
                </span>
                <span>
                  Available <Exact value={r.available_quantity} places={8} />
                </span>
                {r.reserved_quantity !== undefined && (
                  <span>
                    Reserved <Exact value={r.reserved_quantity} places={8} />
                  </span>
                )}
                <strong>
                  Missing <Exact value={r.missing_quantity} places={8} />
                </strong>
                {r.fee_asset && <span>Taker fee asset {r.fee_asset}</span>}
              </div>
            ))}
            {(reasons.length > 0 ||
              route.fundable_with_reallocation !== undefined ||
              route.actionable !== undefined) && (
              <ul className="funding-route-limits">
                {route.fundable_with_reallocation === true && (
                  <li>Different inventory could cover this size.</li>
                )}
                {route.fundable_with_reallocation === false && (
                  <li>No feasible reallocation was shown at this target.</li>
                )}
                {route.actionable === false && <li>Actionability unverified.</li>}
                {reasons.map((reason) => (
                  <li key={reason}>{reason.replaceAll("_", " ")}</li>
                ))}
              </ul>
            )}
          </details>
        );
      })}
      {visible < routes.length && (
        <div className="funding-actions">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setVisible((n) => n + 12)}
          >
            Show more routes
          </Button>
          <span className="terminal-footnote">
            Showing {visible} of {routes.length}
          </span>
        </div>
      )}
    </div>
  );
}
