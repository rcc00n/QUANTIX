"use client";

import { useEffect, useRef, useState } from "react";
import { FileArchive, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Warning } from "@/components/terminal-ui";

type Job = {
  id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  scope: string;
  completed_datasets: number;
  total_datasets: number | null;
  dataset: string | null;
  dataset_rows: number;
  uncompressed_bytes: number;
  bytes?: number;
  file_count?: number;
  error: string | null;
  download_url: string | null;
  coverage_level?: string;
  cancel_requested?: boolean;
};

async function request(path: string, options?: RequestInit) {
  const response = await fetch(`/api/reports/exports${path}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
    ...options,
  });
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 401)
      throw new Error("Session expired. Sign in again before exporting.");
    throw new Error(typeof body.detail === "string" ? body.detail : "Export request failed.");
  }
  return body;
}

export function FullExport({ custom }: { custom: Record<string, string> | null }) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const download = useRef<HTMLAnchorElement>(null);
  const autoDownload = useRef(false);
  const busy = starting || job?.status === "queued" || job?.status === "running";
  useEffect(() => {
    const abort = new AbortController();
    request("/current", { signal: abort.signal })
      .then((value) => setJob(value.job))
      .catch((reason) => {
        if (!abort.signal.aborted) setError(reason.message);
      });
    return () => abort.abort();
  }, []);
  useEffect(() => {
    if (!job || !["running", "queued"].includes(job.status)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let delay = 2000;
    const abort = new AbortController();
    const poll = async () => {
      if (document.hidden) {
        timer = setTimeout(poll, 5000);
        return;
      }
      try {
        const value = await request(`/${job.id}`, { signal: abort.signal });
        if (stopped) return;
        setJob(value);
        setError("");
        delay = 2000;
        if (["running", "queued"].includes(value.status)) timer = setTimeout(poll, delay);
      } catch (reason) {
        if (stopped) return;
        setError(reason instanceof Error ? reason.message : "Connection lost; reconnecting.");
        delay = Math.min(delay * 2, 30000);
        timer = setTimeout(poll, delay);
      }
    };
    timer = setTimeout(poll, delay);
    return () => {
      stopped = true;
      clearTimeout(timer);
      abort.abort();
    };
  }, [job?.id, job?.status]);
  useEffect(() => {
    if (job?.status === "completed" && autoDownload.current && job.download_url) {
      autoDownload.current = false;
      download.current?.click();
    }
  }, [job]);
  async function start(scope: "ALL_RETAINED" | "CUSTOM") {
    setStarting(true);
    setError("");
    try {
      const value = await request("", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(scope === "CUSTOM" ? { scope, ...custom } : {}),
      });
      autoDownload.current = true;
      setJob(value);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start export.");
    } finally {
      setStarting(false);
    }
  }
  return (
    <section className="terminal-panel" aria-label="Complete data export">
      <div className="terminal-workspace-top">
        <div>
          <span className="small-label">INDEPENDENT ANALYSIS · ALL RETAINED DATA</span>
          <p className="terminal-scope-note">
            Every portfolio, period, strategy and monitored asset. One private ZIP with exact
            records, provenance, reconciliation and checksums. Final ZIP ≤512 MB. Shared evidence is
            deduplicated; any raw-data sampling is disclosed. Research capital stays separate.
          </p>
        </div>
        <Button onClick={() => start("ALL_RETAINED")} disabled={busy}>
          <FileArchive size={16} />{" "}
          {starting ? "Starting export…" : "Export Everything for ChatGPT"}
        </Button>
      </div>
      {job && (
        <div role="status" aria-live="polite" className="terminal-footnote">
          {job.status === "queued" && "Export queued. You can continue using QUANTIX."}
          {job.status === "running" && (
            <>
              Generating {job.scope === "CUSTOM" ? "custom report" : "complete archive"} ·{" "}
              {job.completed_datasets}
              {job.total_datasets ? `/${job.total_datasets}` : ""} datasets ·{" "}
              {job.dataset || "Opening consistent snapshot"}
              {job.dataset ? ` · ${job.dataset_rows.toLocaleString()} rows in current dataset` : ""}
              {` · ${(job.uncompressed_bytes / 1048576).toFixed(1)} MiB processed (uncompressed)`}
              <p>
                Runs in the background. Refreshing this page resumes progress. No partial ZIP is
                published.
              </p>
              <Button
                variant="outline"
                disabled={job.cancel_requested}
                onClick={async () => {
                  try {
                    setJob(await request(`/${job.id}/cancel`, { method: "POST", body: "{}" }));
                  } catch (reason) {
                    setError(reason instanceof Error ? reason.message : "Cancel failed");
                  }
                }}
              >
                {job.cancel_requested ? "Cancellation requested…" : "Cancel export"}
              </Button>
            </>
          )}
          {job.status === "completed" && (
            <>
              Complete · {job.file_count} files · {((job.bytes || 0) / 1048576).toFixed(1)} MiB.{" "}
              Download starts automatically; if your browser blocks it, use the button below.
              Available for 24 hours.
              {job.coverage_level === "analysis_snapshot" && (
                <Warning>
                  Analysis snapshot: financial and decision history is complete; some raw market
                  payloads are summarized or sampled. Exact selection counts are in manifest.json.
                </Warning>
              )}
            </>
          )}
          {["failed", "cancelled"].includes(job.status) && (
            <Warning>{job.error || "Export failed. Retry to generate a complete archive."}</Warning>
          )}
        </div>
      )}
      {job?.status === "completed" && job.download_url && (
        <Button asChild variant="outline">
          <a
            ref={download}
            href={job.download_url}
            download={`quantix-${job.scope === "CUSTOM" ? "analysis" : "everything"}-${job.id}.zip`}
          >
            <Download size={15} /> Download ready ZIP
          </a>
        </Button>
      )}
      {error && <Warning>{error}</Warning>}
      <details className="terminal-footnote">
        <summary>Advanced custom-filter export</summary>
        <p>
          Uses the portfolio, applied period and strategy selected below. Its existing size limits
          remain explicit.
        </p>
        <Button variant="outline" disabled={busy || !custom} onClick={() => start("CUSTOM")}>
          Export selected scope
        </Button>
      </details>
    </section>
  );
}
