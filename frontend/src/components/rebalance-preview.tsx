"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Exact, Tag, Warning } from "@/components/terminal-ui";
import type { FundingCatalog } from "@/components/funding-details";
import { assetLabel, utcLabel } from "@/lib/paper";
import { terminalFetch } from "@/lib/terminal";

type RebalanceEstimate = {
  status: "NO_REBALANCE_REQUIRED" | "BLOCKED" | "RESEARCH_TRANSFER_ESTIMATE";
  decision_at: string;
  request: { from_exchange: string; to_exchange: string; asset: string; network: string | null };
  no_rebalance_benchmark: {
    source_available_quantity: string;
    destination_available_quantity: string;
    destination_deficit: string;
    fee_quantity: string;
  };
  hypothetical_transfer: {
    estimated_available_at: string;
    source_debit_quantity: string;
    fee_quantity: string;
    delivered_quantity_after_settlement: string;
    remaining_destination_deficit: string;
    transfer_fee_cad: string | null;
    conversion_cost_cad: string | null;
  } | null;
  blockers: string[];
  risks: string[];
};

export function RebalancePreview({
  sessionId,
  catalog,
}: {
  sessionId: string;
  catalog: FundingCatalog | null;
}) {
  const [result, setResult] = useState<RebalanceEstimate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function preview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const fields = new FormData(event.currentTarget);
    const get = (key: string) => String(fields.get(key) ?? "").trim();
    const settlement = get("settlement_seconds");
    const availability = (key: string) => (get(key) ? get(key) === "available" : null);
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await terminalFetch<RebalanceEstimate>(
        `/paper/sessions/${sessionId}/rebalance/preview`,
        undefined,
        {
          method: "POST",
          body: JSON.stringify({
            from_exchange: get("from_exchange"),
            to_exchange: get("to_exchange"),
            asset: get("asset"),
            target_destination_quantity: get("target_destination_quantity"),
            minimum_source_available: get("minimum_source_available") || "0",
            network: get("network") || null,
            withdrawal_available: availability("withdrawal_available"),
            deposit_available: availability("deposit_available"),
            withdrawal_fee_quantity: get("withdrawal_fee_quantity") || null,
            network_fee_quantity: get("network_fee_quantity") || null,
            settlement_seconds: settlement ? Number(settlement) : null,
            conversion_required: fields.has("conversion_required"),
            conversion_cost_cad: get("conversion_cost_cad") || null,
          }),
        },
      );
      setResult(response);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Rebalance preview unavailable");
    } finally {
      setBusy(false);
    }
  }

  const asset = result ? assetLabel(result.request.asset) : "";
  const transfer = result?.hypothetical_transfer;
  return (
    <details className="planner-inventory funding-rebalance">
      <summary>Research rebalance preview</summary>
      <p className="terminal-footnote">
        Estimate a transfer against holding balances steady. Network, fees and timing are owner
        assumptions. Preview has no effect on balances.
      </p>
      <form onSubmit={preview}>
        <div className="terminal-filterbar">
          <label>
            Source exchange
            <input name="from_exchange" list="funding-venues" required />
          </label>
          <label>
            Destination exchange
            <input name="to_exchange" list="funding-venues" required />
          </label>
          <label>
            Canonical asset
            <select name="asset" required defaultValue="">
              <option value="">Choose asset</option>
              {catalog?.assets.map((item) => (
                <option key={item.id} value={item.canonical_key}>
                  {item.symbol} · {item.canonical_key}
                </option>
              ))}
            </select>
          </label>
          <label>
            Target destination units
            <input name="target_destination_quantity" inputMode="decimal" required />
          </label>
        </div>
        <details className="planner-inventory">
          <summary>Network and cost assumptions</summary>
          <div className="terminal-filterbar">
            <label>
              Network
              <input name="network" />
            </label>
            <label>
              Source withdrawal
              <select name="withdrawal_available" defaultValue="">
                <option value="">Unknown</option>
                <option value="available">Assume available</option>
                <option value="unavailable">Unavailable</option>
              </select>
            </label>
            <label>
              Destination deposit
              <select name="deposit_available" defaultValue="">
                <option value="">Unknown</option>
                <option value="available">Assume available</option>
                <option value="unavailable">Unavailable</option>
              </select>
            </label>
            <label>
              Withdrawal fee · asset units
              <input name="withdrawal_fee_quantity" inputMode="decimal" />
            </label>
            <label>
              Network fee · asset units
              <input name="network_fee_quantity" inputMode="decimal" />
            </label>
            <label>
              Source buffer · asset units
              <input name="minimum_source_available" inputMode="decimal" />
            </label>
            <label>
              Settlement time · seconds
              <input name="settlement_seconds" type="number" min={1} max={604800} step={1} />
            </label>
            <label>
              Conversion cost · CAD
              <input name="conversion_cost_cad" inputMode="decimal" />
            </label>
          </div>
          <label className="funding-boolean">
            <input name="conversion_required" type="checkbox" /> Transfer requires conversion
          </label>
        </details>
        <div className="funding-actions">
          <Button type="submit" variant="outline" disabled={busy}>
            {busy ? "Estimating…" : "Preview rebalance"}
          </Button>
        </div>
      </form>
      {error && <Warning>{error}</Warning>}
      {result && (
        <section className="funding-group" aria-label="Rebalance estimate">
          <div className="section-heading funding-result-heading">
            <h3>
              {asset} · {result.request.from_exchange} → {result.request.to_exchange}
            </h3>
            <Tag value={result.status} />
          </div>
          <p className="terminal-footnote">Snapshot {utcLabel(result.decision_at)}</p>
          <div
            className="table-wrap"
            tabIndex={0}
            role="region"
            aria-label="Scrollable rebalance estimate"
          >
            <table className="terminal-table" aria-label="Rebalance versus no transfer">
              <thead>
                <tr>
                  <th>Funding measure</th>
                  <th>No rebalance</th>
                  <th>Hypothetical transfer</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Destination deficit</td>
                  <td>
                    <Exact value={result.no_rebalance_benchmark.destination_deficit} unit={asset} />
                  </td>
                  <td>
                    <Exact value={transfer?.remaining_destination_deficit} unit={asset} />
                  </td>
                </tr>
                <tr>
                  <td>Destination received</td>
                  <td>
                    <Exact value="0" unit={asset} />
                  </td>
                  <td>
                    <Exact value={transfer?.delivered_quantity_after_settlement} unit={asset} />
                  </td>
                </tr>
                <tr>
                  <td>Source debit</td>
                  <td>
                    <Exact value="0" unit={asset} />
                  </td>
                  <td>
                    <Exact value={transfer?.source_debit_quantity} unit={asset} />
                  </td>
                </tr>
                <tr>
                  <td>Transfer fees</td>
                  <td>
                    <Exact value={result.no_rebalance_benchmark.fee_quantity} unit={asset} />
                  </td>
                  <td>
                    <Exact value={transfer?.fee_quantity} unit={asset} />
                  </td>
                </tr>
                <tr>
                  <td>CAD value of fees</td>
                  <td>
                    <Exact value="0" unit="CAD" />
                  </td>
                  <td>
                    <Exact value={transfer?.transfer_fee_cad} unit="CAD" />
                  </td>
                </tr>
                <tr>
                  <td>Estimated availability</td>
                  <td>Existing balance</td>
                  <td>{utcLabel(transfer?.estimated_available_at)}</td>
                </tr>
              </tbody>
            </table>
          </div>
          {result.blockers.length > 0 && (
            <Warning>
              {result.blockers.map((code) => code.replaceAll("_", " ")).join(" · ")}
            </Warning>
          )}
          <details className="planner-inventory">
            <summary>Assumptions and risks</summary>
            <ul className="funding-route-limits">
              {result.risks.map((code) => (
                <li key={code}>{code.replaceAll("_", " ")}</li>
              ))}
            </ul>
          </details>
        </section>
      )}
    </details>
  );
}
