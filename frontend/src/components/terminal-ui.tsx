"use client";

import { RefreshCw, AlertTriangle, CircleHelp, Activity } from "lucide-react";
import { Button } from "@/components/ui/button";
import { displayDecimal } from "@/lib/opportunities";
import { amount, text } from "@/lib/terminal";
import { utcLabel } from "@/lib/paper";

export function Exact({
  value,
  unit = "",
  places = 4,
  color = false,
}: {
  value: unknown;
  unit?: string;
  places?: number;
  color?: boolean;
}) {
  const v = amount(value);
  if (v === null) return <span className="value-unavailable">Unavailable</span>;
  const display = displayDecimal(v, places);
  return (
    <span
      title={`${v}${unit ? ` ${unit}` : ""}`}
      className={`terminal-exact ${color ? (/^-/.test(v) ? "value-negative" : /^[+]?0*(?:\.0*)?(?:[eE][+-]?\d+)?$/.test(v) ? "" : "value-positive") : ""}`}
    >
      {display.text}
      {unit && <small> {unit}</small>}
    </span>
  );
}
export function Kpi({
  label,
  value,
  unit = "",
  note,
  help,
  color = false,
}: {
  label: string;
  value: unknown;
  unit?: string;
  note?: string;
  help?: string;
  color?: boolean;
}) {
  return (
    <div className="terminal-kpi">
      <div className="kpi-label">
        {label}
        {help && (
          <span title={help} tabIndex={0} aria-label={`${label}: ${help}`}>
            <CircleHelp size={12} />
          </span>
        )}
      </div>
      <strong>
        <Exact value={value} unit={unit} color={color} />
      </strong>
      {note && <span className="kpi-note">{note}</span>}
    </div>
  );
}
export function LiveStatus({
  error,
  loading,
  received,
  refresh,
  interval = 15,
}: {
  error: string;
  loading: boolean;
  received: string | null;
  refresh: () => void;
  interval?: number;
}) {
  return (
    <div className="terminal-live">
      <span className={`live-label ${error ? "live-error" : ""}`} role="status">
        <i className={`status-dot ${error ? "offline" : ""}`} />
        {loading ? "Updating" : error ? "Reconnecting · retained data" : "Connected"}
        <span className="live-cadence"> · {interval}s</span>
      </span>
      <Button
        variant="ghost"
        size="sm"
        onClick={refresh}
        aria-label="Refresh live data"
        title={
          received
            ? `Last browser receipt: ${utcLabel(received)}; source timestamps remain authoritative`
            : "Request latest data"
        }
      >
        <RefreshCw size={14} className={loading ? "spin" : ""} />
      </Button>
    </div>
  );
}
export function Warning({ children }: { children: React.ReactNode }) {
  return (
    <div className="terminal-warning" role="status">
      <AlertTriangle size={16} />
      <div>{children}</div>
    </div>
  );
}
export function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="terminal-empty">
      <Activity size={22} />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
export function Tag({ value, kind }: { value: unknown; kind?: string }) {
  const label = text(value);
  return (
    <span className={`terminal-tag ${kind || label.toLowerCase()}`}>
      {label.replaceAll("_", " ")}
    </span>
  );
}
export function JsonDetails({ title, value }: { title: string; value: unknown }) {
  return (
    <details className="evidence-details">
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}