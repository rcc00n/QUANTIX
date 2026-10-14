"use client";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ChartNoAxesCombined } from "lucide-react";
// Display-only series. Financial calculations must happen in deterministic backend code.
export type EquityPoint = { timestamp: string; equityCad: number };
export function EquityChart({ points = [] }: { points?: EquityPoint[] }) {
  if (!points.length)
    return (
      <div className="empty-chart">
        <div className="chart-grid" aria-hidden="true" />
        <div className="empty-chart-content">
          <span className="empty-icon">
            <ChartNoAxesCombined size={27} />
          </span>
          <h3>Equity history visualization is in development</h3>
          <p>
            Current values are available on Portfolio. Historical visual analysis arrives in Stage
            5.
          </p>
          <span className="small-label">HISTORICAL VISUALIZATION · STAGE 5</span>
        </div>
        <div className="chart-axis" aria-hidden="true">
          <span>UTC</span>
          <span>Inspect Portfolio for recorded values</span>
          <span>CAD</span>
        </div>
      </div>
    );
  return (
    <div className="real-chart">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points}>
          <CartesianGrid stroke="#242d36" vertical={false} />
          <XAxis dataKey="timestamp" stroke="#8b99a8" />
          <YAxis stroke="#8b99a8" unit=" CAD" />
          <Tooltip />
          <Area
            type="linear"
            dataKey="equityCad"
            name="Simulated equity (CAD)"
            stroke="#61dce8"
            fill="#61dce8"
            fillOpacity={0.08}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
