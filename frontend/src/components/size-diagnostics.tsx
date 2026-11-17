"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Exact, JsonDetails, Warning } from "@/components/terminal-ui";
import { terminalFetch, asRecord, text } from "@/lib/terminal";
import { assetLabel, utcLabel } from "@/lib/paper";

type CostsAndReserves = {
  estimated_costs?: {
    fees_cad: string | null;
    fx_drag_cad: string | null;
    slippage_cad: string | null;
    rebalancing_cad: string | null;
  } | null;
  planned_buy_reserve?: string | null;
  planned_sell_reserve?: string | null;
};

type Diagnostics = {
  evaluated_at: string;
  selected_maximum_buy_budget_cad: string;
  best: Record<string, unknown> | null;
  best_costs_and_reserves?: CostsAndReserves;
  best_limiting_constraints?: string[];
  comparisons: (CostsAndReserves & {
    requested_buy_capital_cad: string;
    supported: boolean;
    buy_capital_cad: string | null;
    estimated_net_profit_cad: string | null;
    estimated_net_return_pct: string | null;
    required_sell_inventory: string | null;
    reasons: string[];
    limiting_constraints?: string[];
  })[];
  explanations: string[];
  optimizer: { optimality: string; evaluated_count: number; certified?: boolean };
  market_constraints: Record<string, unknown>;
};

function Costs({ size }: { size?: CostsAndReserves }) {
  const costs = size?.estimated_costs;
  if (!costs) return <>—</>;
  return (
    <ul className="terminal-footnote">
      <li>
        Fees <Exact value={costs.fees_cad} />
      </li>
      <li>
        FX drag <Exact value={costs.fx_drag_cad} />
      </li>
      <li>
        Slippage <Exact value={costs.slippage_cad} />
      </li>
      <li>
        Rebalancing <Exact value={costs.rebalancing_cad} />
      </li>
    </ul>
  );
}

function Reserves({ size, quote, base }: { size?: CostsAndReserves; quote: string; base: string }) {
  if (!size?.planned_buy_reserve || !size.planned_sell_reserve) return <>—</>;
  return (
    <ul className="terminal-footnote">
      <li>
        BUY <Exact value={size.planned_buy_reserve} unit={quote} />
      </li>
      <li>
        SELL <Exact value={size.planned_sell_reserve} unit={base} />
      </li>
    </ul>
  );
}

function constraintsLabel(reasons: string[]) {
  return reasons.map((reason) => reason.replaceAll("_", " ").toLowerCase()).join(" · ");
}

export function SizeDiagnostics({ routeId, budget }: { routeId: string; budget: string }) {
  const [data, setData] = useState<Diagnostics | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setData(null);
    setError("");
  }, [routeId, budget]);
  async function compare() {
    setBusy(true);
    setError("");
    try {
      setData(
        await terminalFetch<Diagnostics>(
          `/opportunities/${routeId}/size-diagnostics?budget_cad=${encodeURIComponent(budget)}`,
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sizing diagnostics unavailable");
    } finally {
      setBusy(false);
    }
  }
  const best = asRecord(data?.best);
  const inventoryUnit = assetLabel(
    text(data?.market_constraints.sell_inventory_currency, "base units"),
  );
  const quoteUnit = text(data?.market_constraints.buy_quote_currency, "quote units");
  return (
    <section className="sizing-diagnostics" aria-label="Trade size diagnostics">
      <div className="section-heading">
        <div>
          <h4>Trade size diagnostics</h4>
          <p className="terminal-footnote">
            Compare seven trade sizes from C$50 to C$5,000 using the retained order books.
          </p>
        </div>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void compare()}>
          {busy ? "Comparing…" : "Compare retained sizes"}
        </Button>
      </div>
      {error && <Warning>{error}</Warning>}
      {data && (
        <>
          <p className="terminal-footnote">
            Historical evaluation {utcLabel(data.evaluated_at)}. Maximum BUY budget{" "}
            <Exact value={data.selected_maximum_buy_budget_cad} unit="CAD" />. Search{" "}
            {data.optimizer.optimality.replaceAll("_", " ")} · {data.optimizer.evaluated_count}{" "}
            evaluations. These are projections; no fills are created.
          </p>
          <div className="table-scroll">
            <table className="terminal-table" aria-label="Retained size comparison">
              <thead>
                <tr>
                  <th>Requested BUY · CAD</th>
                  <th>Actual BUY · CAD</th>
                  <th>Estimated net · CAD</th>
                  <th>Net return · %</th>
                  <th>SELL inventory · {inventoryUnit}</th>
                  <th>Estimated costs · CAD</th>
                  <th>Paper reserves</th>
                  <th>Binding constraints</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{data.optimizer.certified ? "Certified optimum" : "Best evaluated size"}</td>
                  <td>
                    <Exact value={best.acquisition_cost_cad} />
                  </td>
                  <td>
                    <Exact value={best.net_profit_cad} />
                  </td>
                  <td>
                    <Exact value={best.net_return_pct} />
                  </td>
                  <td>
                    <Exact value={best.sell_base_required} />
                  </td>
                  <td>
                    <Costs size={data.best_costs_and_reserves} />
                  </td>
                  <td>
                    <Reserves
                      size={data.best_costs_and_reserves}
                      quote={quoteUnit}
                      base={inventoryUnit}
                    />
                  </td>
                  <td>
                    {constraintsLabel(data.best_limiting_constraints ?? []) ||
                      (data.best ? "Depth supported" : "Unavailable")}
                  </td>
                </tr>
                {data.comparisons.map((row) => (
                  <tr key={row.requested_buy_capital_cad}>
                    <td>
                      <Exact value={row.requested_buy_capital_cad} />
                    </td>
                    <td>
                      <Exact value={row.buy_capital_cad} />
                    </td>
                    <td>
                      <Exact value={row.estimated_net_profit_cad} />
                    </td>
                    <td>
                      <Exact value={row.estimated_net_return_pct} />
                    </td>
                    <td>
                      <Exact value={row.required_sell_inventory} />
                    </td>
                    <td>
                      <Costs size={row} />
                    </td>
                    <td>
                      <Reserves size={row} quote={quoteUnit} base={inventoryUnit} />
                    </td>
                    <td title={constraintsLabel(row.reasons)}>
                      {constraintsLabel(row.limiting_constraints ?? row.reasons) ||
                        (row.supported ? "Depth supported" : "Unavailable")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="notification-reasons">
            {data.explanations.map((reason) => (
              <span key={reason}>{reason.replaceAll("_", " ")}</span>
            ))}
          </div>
          <p className="terminal-footnote">
            Net profit uses directional FX cash flows. FX drag estimates round-trip conversion cost.
            Paper reserves show the quote and base units needed at each venue.
          </p>
          <JsonDetails
            title="Pinned precision, minimums, fee units and market constraints"
            value={data.market_constraints}
          />
        </>
      )}
    </section>
  );
}
