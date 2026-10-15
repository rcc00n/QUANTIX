"use client";

import { useState } from "react";
import Link from "next/link";
import { FileSearch, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, Exact, JsonDetails, SectionTitle, Tag, Warning } from "@/components/terminal-ui";
import { terminalFetch, useLive } from "@/lib/terminal";
import type { PaperSession } from "@/lib/paper";
import type { OpportunityPage } from "@/lib/opportunities";

type AiStatus = {
  enabled: boolean;
  provider: string;
  external_calls_active: boolean;
  deterministic_fallback: boolean;
  billing: string;
};
type Explanation = {
  status: "disabled" | "unavailable" | "complete";
  text: string;
  provider: string;
  reason: string | null;
  cached: boolean;
  deterministic: boolean;
  numeric_inputs: Record<string, string | null>;
  source_id: string;
  source_type: "opportunity" | "paper_report";
  attribution: Record<string, unknown>;
  classification: string;
  limitations: string[];
};

export function AiInsightsWorkspace() {
  const status = useLive<AiStatus>("/ai/status", 60000);
  const sessions = useLive<{ items: PaperSession[] }>("/paper/sessions", 60000);
  const opportunities = useLive<OpportunityPage>("/opportunities?history=true&limit=20", 60000);
  const [sourceType, setSourceType] = useState("paper_report");
  const [sourceId, setSourceId] = useState("");
  const [result, setResult] = useState<Explanation | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const options =
    sourceType === "paper_report"
      ? (sessions.data?.items || []).map((session) => ({
          id: session.id,
          label: `${session.name} · ${session.classification}`,
        }))
      : (opportunities.data?.items || []).map((item) => ({
          id: item.id,
          label: `${item.base_asset} · ${item.buy_exchange} → ${item.sell_exchange} · ${item.status}`,
        }));
  const selectedId = options.some((option) => option.id === sourceId)
    ? sourceId
    : options[0]?.id || "";
  async function explain() {
    if (!selectedId) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(
        await terminalFetch<Explanation>("/ai/explain", undefined, {
          method: "POST",
          body: JSON.stringify(
            sourceType === "paper_report"
              ? { session_id: selectedId }
              : { opportunity_id: selectedId },
          ),
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Explanation unavailable");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <section className="terminal-panel">
        <SectionTitle eyebrow="EVIDENCE BEFORE INTERPRETATION" title="Quantitative explanations">
          <Tag
            value={
              status.data?.external_calls_active
                ? "Optional NVIDIA enabled"
                : "External AI disabled"
            }
          />
        </SectionTitle>
        {status.error && <Warning>{status.error}</Warning>}
        <p className="terminal-description">
          Explain retained opportunity evidence or one independent paper portfolio. Server-authored
          explanations remain available when the optional provider is disabled.
        </p>
        <div className="terminal-filterbar insight-controls">
          <label>
            Evidence type
            <select
              aria-label="Explanation evidence type"
              value={sourceType}
              onChange={(e) => {
                setSourceType(e.target.value);
                setSourceId("");
                setResult(null);
              }}
            >
              <option value="paper_report">Paper portfolio report</option>
              <option value="opportunity">Retained opportunity</option>
            </select>
          </label>
          <label>
            Evidence source
            <select
              aria-label="Explanation evidence source"
              value={selectedId}
              onChange={(e) => {
                setSourceId(e.target.value);
                setResult(null);
              }}
            >
              <option value="" disabled>
                Select recorded evidence
              </option>
              {options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <Button disabled={busy || !selectedId || !status.data} onClick={() => void explain()}>
            <FileSearch size={14} />
            {busy ? "Reading evidence…" : "Explain selected evidence"}
          </Button>
        </div>
        {(sourceType === "paper_report" ? sessions.error : opportunities.error) && (
          <Warning>{sourceType === "paper_report" ? sessions.error : opportunities.error}</Warning>
        )}
        <p className="terminal-footnote">
          {status.data?.billing || "No paid usage is enabled."} Provider configuration is
          server-side. Explanations cannot change quantitative rules, fills, balances or journals.
        </p>
      </section>
      {error && <Warning>{error}</Warning>}
      {result ? (
        <section className="terminal-panel">
          <SectionTitle
            eyebrow="RETAINED APPLICATION EVIDENCE"
            title={
              result.source_type === "paper_report"
                ? "Paper performance explanation"
                : "Opportunity explanation"
            }
          >
            <Tag value={result.classification} />
          </SectionTitle>
          <div className="settings-version-line">
            <span>
              {result.provider === "none" || result.status !== "complete"
                ? "Deterministic fallback"
                : `Provider ${result.provider}`}
            </span>
            <span>{result.cached ? "Cached explanation" : "Computed explanation"}</span>
            {result.reason && <span>{result.reason.replaceAll("_", " ")}</span>}
          </div>
          <p className="insight-explanation">{result.text}</p>
          <details className="evidence-details">
            <summary>Exact numeric inputs</summary>
            <dl className="terminal-definition-list">
              {Object.entries(result.numeric_inputs).map(([key, value]) => (
                <div key={key}>
                  <dt>{key.replaceAll("_", " ")}</dt>
                  <dd>
                    <Exact value={value} places={8} />
                  </dd>
                </div>
              ))}
            </dl>
          </details>
          <JsonDetails
            title="Source attribution and limitations"
            value={{
              source_id: result.source_id,
              source_type: result.source_type,
              attribution: result.attribution,
              limitations: result.limitations,
            }}
          />
        </section>
      ) : (
        <section className="terminal-panel">
          <Empty title="Select recorded evidence">
            No model-generated market data or financial recommendations are shown. Choose a source
            to inspect why a quantitative outcome occurred.
          </Empty>
        </section>
      )}
      <section className="terminal-panel">
        <SectionTitle eyebrow="CONTINUE WITH THE RECORDS" title="Inspect underlying evidence">
          <Sparkles size={17} />
        </SectionTitle>
        <div className="insight-evidence-list">
          <Link href="/opportunities">
            Opportunity Radar · depth, fees, FX and size constraints
          </Link>
          <Link href="/analytics">Analytics · scoped simulated performance and exports</Link>
          <Link href="/paper-trading">
            Paper Trading · virtual orders, fills and ledger evidence
          </Link>
          <Link href="/system-health">System Health · source confidence and clock trust</Link>
        </div>
      </section>
    </>
  );
}
