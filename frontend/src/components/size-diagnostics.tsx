"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Exact, JsonDetails, Warning } from "@/components/terminal-ui";
import { terminalFetch, asRecord, text } from "@/lib/terminal";
import { assetLabel, utcLabel } from "@/lib/paper";

type Diagnostics = {
  evaluated_at: string;
  selected_maximum_buy_budget_cad: string;
  best: Record<string, unknown> | null;
  comparisons: {
    requested_buy_capital_cad: string;
    supported: boolean;
    buy_capital_cad: string | null;
    estimated_net_profit_cad: string | null;
    estimated_net_return_pct: string | null;
    required_sell_inventory: string | null;
    reasons: string[];
  }[];
  explanations: string[];
  optimizer: { optimality: string; evaluated_count: number };
  market_constraints: Record<string, unknown>;
};
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
  return (
    <section className="sizing-diagnostics" aria-label="Trade size diagnostics">
      <div className="section-heading">
        <div>
          <h4>Trade size diagnostics</h4>
          <p className="terminal-footnote">
            Compare retained depth at C$50 / 100 / 250 / 500 / 1,000 and the best projected amount.
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
                  <th>Support</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Best within budget</td>
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
                  <td>{data.best ? "Projected optimum" : "Unavailable"}</td>
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
                    <td title={row.reasons.join(" · ")}>
                      {row.supported
                        ? "Depth supported"
                        : row.reasons.map((x) => x.replaceAll("_", " ")).join(" · ") ||
                          "Unavailable"}
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
            The optimizer maximizes estimated net CAD profit within the budget. Larger purchases can
            have worse prices or insufficient depth. Depth support does not establish verified fees,
            FX, account eligibility or actionability.
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
