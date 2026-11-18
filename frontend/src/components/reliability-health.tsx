"use client";

import Link from "next/link";
import { Empty, SectionTitle, Tag, Warning } from "@/components/terminal-ui";
import type { NotificationStatus } from "@/lib/notifications";
import { utcLabel } from "@/lib/paper";
import { amount, asRecord, text, useLive } from "@/lib/terminal";

const checks = {
  market_coverage: "Market-data freshness",
  scanner_progress: "Scanner progress",
  native_streams: "Native WebSocket synchronization",
  clock_trust: "Clock trust monitor",
} as const;
type CheckKey = keyof typeof checks;
type CheckState = "UNKNOWN" | "HEALTHY" | "DEGRADED" | "DISABLED";
type ReliabilityCheck = {
  key: CheckKey;
  state: CheckState;
  reason: string | null;
  pending_state: "HEALTHY" | "DEGRADED" | null;
  pending_seconds: number | null;
  metrics: Record<string, unknown>;
};
export type ReliabilitySnapshot = {
  schema_version: "quantix-collector-reliability-v1";
  observed_at: string | null;
  checks: ReliabilityCheck[];
};

function measurement({ key, metrics }: ReliabilityCheck): string {
  switch (key) {
    case "market_coverage":
      return `${text(metrics.fresh_books, "—")} of ${text(metrics.monitored_books, "—")} fresh · ${text(metrics.aging_books, "—")} aging · ${text(metrics.stale_books, "—")} stale · ${text(metrics.unavailable_books, "—")} unavailable · ${text(metrics.clock_untrusted_books, "—")} clock untrusted`;
    case "scanner_progress":
      return `${text(metrics.pending_comparisons, "—")} / ${text(metrics.queue_capacity, "—")} pending · oldest ${text(metrics.oldest_seconds, "—")} s · heartbeat age ${text(metrics.heartbeat_age_seconds, "—")} s · ${text(metrics.deferred_markets, "—")} markets deferred`;
    case "native_streams": {
      const venues = Array.isArray(metrics.unsynchronized_exchanges)
        ? metrics.unsynchronized_exchanges.join(", ")
        : "";
      return `${text(metrics.synchronized_markets, "—")} / ${text(metrics.subscribed_markets, "—")} synchronized${venues ? ` · Resync needed: ${venues}` : ""}`;
    }
    case "clock_trust":
      return `Application UTC ${metrics.trusted === true ? "trusted" : metrics.trusted === false ? "untrusted" : "unverified"} · host synchronization ${metrics.host_healthy === true ? "healthy" : metrics.host_healthy === false ? "unhealthy" : "unverified"}`;
  }
}

function reason(value: string) {
  return value.replaceAll("_", " ").toLowerCase();
}

export function CollectorReliability({
  snapshot,
  stale,
}: {
  snapshot: ReliabilitySnapshot | undefined;
  stale: boolean;
}) {
  return (
    <section className="terminal-panel" aria-label="Collector reliability">
      <SectionTitle title="Collector reliability" />
      {snapshot?.schema_version === "quantix-collector-reliability-v1" &&
      Array.isArray(snapshot.checks) &&
      snapshot.checks.length ? (
        <>
          <p className="terminal-scope-note">
            {stale ? "Retained check snapshot" : "Checks sampled"} ·{" "}
            {utcLabel(snapshot.observed_at)}
          </p>
          <div className="table-wrap">
            <table className="terminal-table" aria-label="Sustained collector checks">
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">State</th>
                  <th scope="col">Measurement</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.checks.map((item) => (
                  <tr key={item.key}>
                    <th scope="row">{checks[item.key]}</th>
                    <td>
                      <Tag value={item.state} />
                      {item.reason && (
                        <small className="eligibility-note">{reason(item.reason)}</small>
                      )}
                      {item.pending_state && (
                        <small className="eligibility-note">
                          {item.pending_state === "HEALTHY"
                            ? "Confirming recovery"
                            : "Confirming degradation"}
                          {item.pending_seconds !== null && ` · ${item.pending_seconds} s`}
                        </small>
                      )}
                    </td>
                    <td>{measurement(item)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <Empty title="Reliability checks unavailable">
          Waiting for the collector check snapshot.
        </Empty>
      )}
    </section>
  );
}

export function TelegramWorkerHealth({ clockTrusted }: { clockTrusted: boolean }) {
  const status = useLive<NotificationStatus>("/notifications/status", 60000);
  const current = status.stale ? null : status.data;
  const worker = current?.worker;
  let health = status.loading ? "Checking…" : "Unavailable";
  if (worker?.webhook_conflict) health = "Polling blocked by webhook conflict";
  else if (worker && !clockTrusted) health = "Heartbeat freshness unverified";
  else if (worker) health = worker.healthy ? "Healthy" : "Degraded";
  const policy = !current
    ? "Unavailable"
    : !current.telegram_enabled
      ? "Disabled by policy"
      : current.delivery_active && clockTrusted
        ? "Active"
        : "Enabled, delivery unconfirmed";
  return (
    <section className="terminal-panel" aria-label="Telegram worker health">
      <SectionTitle title="Telegram worker health">
        <Link href="/signals" className="text-link">
          Delivery controls
        </Link>
      </SectionTitle>
      {status.error && <Warning>{status.error}</Warning>}
      <dl className="terminal-definition-list">
        <div>
          <dt>Worker</dt>
          <dd>{health}</dd>
        </div>
        <div>
          <dt>Delivery status</dt>
          <dd>{policy}</dd>
        </div>
        <div>
          <dt>Last reported heartbeat</dt>
          <dd>{utcLabel(status.data?.worker?.heartbeat_at)}</dd>
        </div>
        {worker?.error_code && (
          <div>
            <dt>Reported issue</dt>
            <dd>{reason(worker.error_code)}</dd>
          </div>
        )}
      </dl>
      <p className="terminal-scope-note">
        Enabled daily or weekly reports can include recorded system failures. Immediate system
        failure delivery is unavailable in this release.
      </p>
    </section>
  );
}

export function PaperHealth({
  paper,
  valuation,
  stale,
}: {
  paper: Record<string, unknown>;
  valuation: Record<string, unknown>;
  stale: boolean;
}) {
  const metrics = asRecord(paper.metrics);
  const counts = asRecord(valuation.valuation_counts);
  const hasCounters = paper.enabled === true && paper.metrics != null && !stale;
  return (
    <section className="terminal-panel" aria-label="Paper execution and valuation">
      <SectionTitle title="Paper execution and valuation" />
      <div className="terminal-split">
        <dl className="terminal-definition-list">
          <div>
            <dt>Worker cycle errors</dt>
            <dd>{hasCounters ? text(metrics.failed_ticks, "0") : "Unavailable"}</dd>
          </div>
          <div>
            <dt>Cycles blocked by UTC gate</dt>
            <dd>{hasCounters ? text(metrics.clock_gated_ticks, "0") : "Unavailable"}</dd>
          </div>
          <div>
            <dt>Failed / partial / missed paper outcomes</dt>
            <dd>
              {hasCounters
                ? [metrics.failed, metrics.partial, metrics.missed]
                    .map((value) => text(value, "0"))
                    .join(" / ")
                : "Unavailable"}
            </dd>
          </div>
          <div>
            <dt>Rejected market observations</dt>
            <dd>{hasCounters ? text(metrics.rejected_observations, "0") : "Unavailable"}</dd>
          </div>
        </dl>
        <dl className="terminal-definition-list">
          <div>
            <dt>Active owner portfolios checked</dt>
            <dd>{text(valuation.portfolios_sampled, "Unavailable")}</dd>
          </div>
          <div>
            <dt>Complete at checkpoint / partial / stale / unknown</dt>
            <dd>
              {Object.keys(counts).length
                ? [counts.COMPLETE_FRESH, counts.PARTIAL, counts.STALE, counts.UNKNOWN]
                    .map((value) => text(value, "—"))
                    .join(" / ")
                : "Unavailable"}
            </dd>
          </div>
          <div>
            <dt>Latest retained valuation</dt>
            <dd>{utcLabel(amount(valuation.latest_checkpoint_at))}</dd>
          </div>
        </dl>
      </div>
      <p className="terminal-scope-note">
        Worker counters cover this collector run. Valuation counts come from retained checkpoints
        for active portfolios and have their own timestamps.
        {valuation.truncated === true && " The valuation sample reached its portfolio limit."}
      </p>
    </section>
  );
}
