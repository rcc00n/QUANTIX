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