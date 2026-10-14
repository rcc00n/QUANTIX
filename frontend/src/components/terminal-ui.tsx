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