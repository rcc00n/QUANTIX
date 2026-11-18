"use client";

import { Empty, Exact, SectionTitle, Warning } from "@/components/terminal-ui";
import { utcLabel } from "@/lib/paper";
import { amount, asRecord, asRecords, text } from "@/lib/terminal";

const warnings = {
  EXPORT_SCRATCH_BELOW_DISK_RESERVE: "Export scratch space is below its safety reserve.",
  UNUSUAL_DATABASE_GROWTH: "The latest database growth interval exceeded its sampled baseline.",
  POSTGRES_INDEX_NOT_READY_OR_VALID:
    "PostgreSQL reports indexes that are invalid or still building.",
} as const;

export function StorageHealth({ snapshot, stale }: { snapshot: unknown; stale: boolean }) {
  const value = asRecord(snapshot);
  const database = asRecord(value.database);
  const growth = asRecord(value.database_growth);
  const disk = asRecord(value.export_scratch);
  const relations = asRecords(database.retained_relations);
  const activeWarnings = Array.isArray(value.warnings)
    ? Object.keys(warnings).filter((key) => (value.warnings as unknown[]).includes(key))
    : [];
  return (
    <section className="terminal-panel" aria-label="Database storage and backup health">
      <SectionTitle title="Database storage and backup health" />
      {value.schema_version === "quantix-storage-health-v1" ? (
        <>
          <p className="terminal-scope-note">
            {stale
              ? "Retained measurement"
              : value.database_state === "OBSERVED"
                ? "PostgreSQL catalog sample"
                : "PostgreSQL storage sample unavailable"}{" "}
            · {utcLabel(amount(database.observed_at))} · sample age{" "}
            {text(value.sample_age_seconds, "—")} s
          </p>
          {!stale &&
            activeWarnings.map((key) => (
              <Warning key={key}>{warnings[key as keyof typeof warnings]}</Warning>
            ))}
          <dl className="terminal-definition-list">
            <div>
              <dt>Current database size</dt>
              <dd>
                <Exact value={database.database_bytes} places={0} unit="bytes" />
              </dd>
            </div>
            <div>
              <dt>Observed database growth</dt>
              <dd>
                <Exact value={growth.database_bytes_per_day} places={0} unit="bytes/day" />
                {growth.window_seconds != null && (
                  <small>
                    {" "}
                    · {text(growth.sample_count)} samples over {text(growth.window_seconds)} s
                  </small>
                )}
              </dd>
            </div>
            <div>
              <dt>Cluster WAL generated</dt>
              <dd>
                <Exact value={database.wal_bytes} places={0} unit="bytes" />
              </dd>
            </div>
            <div>
              <dt>Observed cluster WAL rate</dt>
              <dd>
                <Exact value={growth.wal_generated_bytes_per_day} places={0} unit="bytes/day" />
              </dd>
            </div>
            <div>
              <dt>DB-volume runway</dt>
              <dd>Unavailable · database filesystem free space is outside this process</dd>
            </div>
            <div>
              <dt>Export scratch free / reserve</dt>
              <dd>
                <Exact value={disk.free_bytes} places={0} unit="bytes" /> /{" "}
                <Exact value={disk.reserve_bytes} places={0} unit="bytes" />
              </dd>
            </div>
            <div>
              <dt>Backup health</dt>
              <dd>Unavailable · backup and restore proofs are verified outside the API</dd>
            </div>
            <div>
              <dt>Invalid or building indexes</dt>
              <dd>
                {text(database.unhealthy_indexes, "—")} / {text(database.indexes, "—")}
              </dd>
            </div>
            <div>
              <dt>Visible query waits / active over 30 s</dt>
              <dd>
                {text(database.visible_lock_waits, "—")} /{" "}
                {text(database.visible_queries_over_30s, "—")}
                <small> · {text(database.visible_sessions, "—")} visible peer sessions</small>
              </dd>
            </div>
            <div>
              <dt>Query temporary writes / deadlocks</dt>
              <dd>
                <Exact value={database.temp_bytes} places={0} unit="bytes" /> /{" "}
                {text(database.deadlocks, "—")}
              </dd>
            </div>
          </dl>
          {relations.length > 0 && (
            <div className="table-wrap">
              <table
                className="terminal-table"
                aria-label="Retained relation and index measurements"
              >
                <thead>
                  <tr>
                    <th scope="col">Retained relation</th>
                    <th scope="col">Total bytes</th>
                    <th scope="col">Index bytes</th>
                    <th scope="col">Estimated rows</th>
                    <th scope="col">Seq / index scans</th>
                  </tr>
                </thead>
                <tbody>
                  {relations.map((row) => (
                    <tr key={text(row.name)}>
                      <th scope="row">{text(row.name).replaceAll("_", " ")}</th>
                      <td>
                        <Exact value={row.bytes} places={0} />
                      </td>
                      <td>
                        <Exact value={row.index_bytes} places={0} />
                      </td>
                      <td>
                        <Exact value={row.estimated_rows} places={0} />
                      </td>
                      <td>
                        {text(row.sequential_scans, "—")} / {text(row.index_scans, "—")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="terminal-description">
            WAL counters measure generated bytes since {utcLabel(amount(database.wal_stats_since))}.
            Query temporary writes and deadlocks are cumulative since{" "}
            {utcLabel(amount(database.database_stats_since))}. Volume-confirmed disk runway includes
            the database filesystem reserve.
          </p>
        </>
      ) : (
        <Empty title="Storage telemetry unavailable">Waiting for a storage sample.</Empty>
      )}
    </section>
  );
}
