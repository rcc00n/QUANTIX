"use client";

import { useState } from "react";
import { BookDepth } from "@/components/terminal-charts";
import { Exact, Empty, Tag, JsonDetails, Warning } from "@/components/terminal-ui";
import { asRecord, asRecords, text, amount, terminalFetch } from "@/lib/terminal";
import type { PaperTradeDetail } from "@/lib/paper";
import { utcLabel, assetLabel } from "@/lib/paper";

export function TradeInspector({ detail }: { detail: PaperTradeDetail }) {
  const events = asRecords(detail.events);
  const orders = asRecords(detail.orders);
  const fills = asRecords(detail.fills);
  const observations = asRecords(detail.observations);
  const ledger = asRecords(detail.ledger_entries);
  const accounts = asRecords(detail.accounts);
  const state = asRecord(detail.trade.state);
  const assumptions = asRecord(detail.trade.assumptions);
  const [replay, setReplay] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function runReplay() {
    setBusy(true);
    setError("");
    try {
      setReplay(
        await terminalFetch("/paper/replay", undefined, {
          method: "POST",
          body: JSON.stringify({
            evaluation_id: detail.trade.evaluation_id,
            reaction_delay_seconds: Number(detail.trade.reaction_delay_ms ?? 30000) / 1000,
            budget_cad: asRecord(assumptions.strategy).trade_amount_cad || "250",
          }),
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Replay unavailable");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="trade-inspector">
      <div className="terminal-kpi-grid secondary-kpis">
        <div>
          <span className="small-label">SIMULATION CLASS</span>
          <Tag value={detail.trade.classification} />
        </div>
        <div>
          <span className="small-label">RESPONSE DELAY</span>
          <strong>{text(detail.trade.reaction_delay_ms, "—")} ms</strong>
        </div>
        <div>
          <span className="small-label">OUTCOME</span>
          <Tag value={detail.trade.status} />
        </div>
        <div>
          <span className="small-label">STRATEGY VERSION</span>
          <strong>{text(detail.trade.strategy_version_id).slice(0, 8)}</strong>
        </div>
      </div>
      <p className="terminal-footnote">
        Detected/requested {utcLabel(detail.trade.requested_at)} · eligible after{" "}
        {utcLabel(detail.trade.eligible_at)} · completed {utcLabel(detail.trade.completed_at)}
      </p>
      <ol className="execution-timeline">
        {events.map((event, i) => (
          <li key={text(event.id, String(i))}>
            <span className="timeline-node" />
            <div>
              <strong>
                {text(event.event_type || event.status, "Recorded event").replaceAll("_", " ")}
              </strong>
              <time>{utcLabel(amount(event.occurred_at || event.created_at))}</time>
              <p>{text(event.reason || event.evidence, "")}</p>
              <JsonDetails title="Exact event provenance" value={event.provenance} />
            </div>
          </li>
        ))}
      </ol>
      <div className="terminal-split">
        {orders.map((order, i) => {
          const orderFills = fills.filter((x) => x.order_id === order.id);
          const observation = observations.find((x) => x.order_id === order.id);
          const observedBook = asRecord(asRecord(observation?.payload).book);
          const base = assetLabel(text(observedBook.base_key, "Unknown base"));
          const quote = assetLabel(text(observedBook.quote_key, "Unknown quote"));
          return (
            <section className="execution-leg" key={text(order.id, String(i))}>
              <div className="section-heading">
                <h4>Simulated {text(order.side).toUpperCase()} leg</h4>
                <Tag value={order.status} />
              </div>
              <p className="terminal-footnote">
                Venue {text(observedBook.exchange)} · market {text(observedBook.symbol)} · quality{" "}
                {text(observedBook.state)} · source {text(observedBook.source)}
              </p>
              <p className="terminal-footnote">
                Available {utcLabel(amount(observation?.received_at || observedBook.received_at))} ·
                exchange timestamp{" "}
                {utcLabel(amount(observation?.source_at || observedBook.exchange_timestamp))}
              </p>
              <dl className="terminal-definition-list">
                <div>
                  <dt>Price denomination</dt>
                  <dd>
                    {quote} per {base}
                  </dd>
                </div>
                <div>
                  <dt>Requested quantity</dt>
                  <dd>
                    <Exact value={order.quantity} unit={base} places={8} />
                  </dd>
                </div>
                <div>
                  <dt>Outcome reason</dt>
                  <dd>{text(order.reason, "No rejection reason")}</dd>
                </div>
              </dl>
              {orderFills.length ? (
                orderFills.map((fill, j) => {
                  const feeVersion = asRecords(detail.fee_versions).find(
                    (x) => x.id === fill.fee_version_id,
                  );
                  const feeCurrency =
                    asRecord(feeVersion?.assumptions).currency ??
                    asRecord(asRecord(state.legs)[String(order.side)]).fee_currency;
                  const feeUnit =
                    feeCurrency === "base"
                      ? base
                      : feeCurrency === "quote"
                        ? quote
                        : "Unknown fee asset";
                  return (
                    <dl className="terminal-definition-list" key={text(fill.id, String(j))}>
                      <div>
                        <dt>Observed simulated fill</dt>
                        <dd>
                          <Exact value={fill.quantity} unit={base} places={8} />
                        </dd>
                      </div>
                      <div>
                        <dt>Modeled execution price</dt>
                        <dd>
                          <Exact value={fill.price} unit={quote} places={8} />
                        </dd>
                      </div>
                      <div>
                        <dt>Fee units</dt>
                        <dd>
                          <Exact value={fill.fee_quantity} unit={feeUnit} places={8} />
                        </dd>
                      </div>
                      <div>
                        <dt>Fee CAD</dt>
                        <dd>
                          <Exact value={fill.fee_cad} unit="CAD" />
                        </dd>
                      </div>
                      <div>
                        <dt>Executed at</dt>
                        <dd>{utcLabel(amount(fill.executed_at))}</dd>
                      </div>
                      <div>
                        <dt>Journal</dt>
                        <dd>{text(fill.journal_id).slice(0, 8)}</dd>
                      </div>
                      <JsonDetails title="Fill, cost basis and FX provenance" value={fill} />
                    </dl>
                  );
                })
              ) : (
                <Empty title="No simulated fill for this leg">
                  Missing or invalid depth never becomes an execution.
                </Empty>
              )}
            </section>
          );
        })}
      </div>
      {!orders.length && (
        <Empty title="No simulated orders">
          The candidate did not reach execution eligibility. Its rejection and inventory limitations
          remain recorded.
        </Empty>
      )}
      {observations.slice(0, 4).map((observation, i) => {
        const payload = asRecord(observation.payload);
        const book = asRecord(payload.book || payload.observation || payload);
        const normalized = asRecord(book.normalized);
        const bids = asRecords(normalized.bids).map((x) => ({
          price: text(x.price, "0"),
          quantity: text(x.quantity, "0"),
        }));
        const asks = asRecords(normalized.asks).map((x) => ({
          price: text(x.price, "0"),
          quantity: text(x.quantity, "0"),
        }));
        return (
          <section key={text(observation.id, String(i))}>
            <h4>Execution depth evidence · {text(book.exchange || book.symbol, "Observation")}</h4>
            <p className="terminal-footnote">
              Available {utcLabel(amount(observation.received_at || book.received_at))} · source{" "}
              {utcLabel(amount(observation.source_at || book.exchange_timestamp))}
            </p>
            <BookDepth label="Execution evidence" bids={bids} asks={asks} />
            <JsonDetails
              title="Captured book, UTC uncertainty and available FX"
              value={observation}
            />
          </section>
        );
      })}
      {ledger.length > 0 && (
        <section>
          <h4>Immutable ledger movements</h4>
          <div className="table-wrap">
            <table className="terminal-table" aria-label="Simulation ledger movements">
              <thead>
                <tr>
                  <th>Journal</th>
                  <th>Account</th>
                  <th>Units</th>
                  <th>CAD annotation</th>
                  <th>Memo</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((entry, i) => {
                  const account = accounts.find((x) => x.id === entry.account_id);
                  return (
                    <tr key={text(entry.id, String(i))}>
                      <td>{text(entry.journal_id).slice(0, 8)}</td>
                      <td>{text(account?.role || entry.account_id)}</td>
                      <td>
                        <Exact value={entry.quantity} places={8} />
                      </td>
                      <td>
                        <Exact value={entry.cad_value} unit="CAD" />
                      </td>
                      <td>{text(entry.memo, "")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <JsonDetails title="Final calculated outcome and remaining exposure" value={state} />
      <JsonDetails
        title="Pinned fees, conversions and initial benchmark"
        value={{
          assumptions,
          fees: detail.fee_versions,
          fx: detail.fx_quotes,
          session: detail.session_provenance,
        }}
      />
      {detail.trade.evaluation_id && (
        <div className="replay-controls">
          <button className="text-link" disabled={busy} onClick={() => void runReplay()}>
            {busy ? "Checking retained evidence…" : "Assess causal historical replay"}
          </button>
          <p className="terminal-footnote">
            Read-only research projection. Sparse retained evidence produces a limitation, never
            invented fills.
          </p>
        </div>
      )}
      {error && <Warning>{error}</Warning>}
      {replay && (
        <>
          <Warning>
            {(Array.isArray(replay.limitations) ? replay.limitations : [])
              .map(String)
              .join(" · ") || "Research projection only; no portfolio or fill was created."}
          </Warning>
          <JsonDetails title="Deterministic retained-evidence replay result" value={replay} />
        </>
      )}
    </div>
  );
}
