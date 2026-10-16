"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Empty } from "@/components/terminal-ui";
import { plot, text } from "@/lib/terminal";
import { utcLabel } from "@/lib/paper";

export type SeriesPoint = {
  as_of: string;
  equity_cad?: string | null;
  no_trading_benchmark_cad?: string | null;
  net_simulated_pnl_cad?: string | null;
  realized_simulated_pnl_cad?: string | null;
  unrealized_pnl_cad?: string | null;
  drawdown_pct?: string | null;
  capital_utilization_pct?: string | null;
  [key: string]: unknown;
};
const stroke = "#33404f";
export function HistoryChart({
  points,
  kind = "equity",
}: {
  points: SeriesPoint[];
  kind?: "equity" | "pnl" | "drawdown";
}) {
  const keys =
    kind === "equity"
      ? ["equity_cad", "no_trading_benchmark_cad"]
      : kind === "pnl"
        ? ["net_simulated_pnl_cad", "realized_simulated_pnl_cad"]
        : ["drawdown_pct"];
  const rows: Record<string, unknown>[] = points.map((x) => ({
    ...x,
    time: Date.parse(x.as_of),
    ...Object.fromEntries(keys.map((k) => [`${k}_plot`, plot(x[k])])),
  }));
  if (rows.filter((x) => keys.some((k) => plot(x[k]) !== null)).length < 2)
    return (
      <Empty title="More recorded history needed">
        At least two valued checkpoints are required. No chart points are synthesized.
      </Empty>
    );
  const labels: Record<string, string> = {
    equity_cad: "Simulated equity",
    no_trading_benchmark_cad: "No-trading benchmark",
    net_simulated_pnl_cad: "Total portfolio change",
    realized_simulated_pnl_cad: "Realized simulated P&L",
    drawdown_pct: "Drawdown",
  };
  const colors = ["#66deec", "#9d97ef"];
  return (
    <div
      className="terminal-chart"
      role="img"
      aria-label={`${kind} chart from recorded checkpoints`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 14, right: 8, bottom: 5, left: 0 }}>
          <CartesianGrid stroke={stroke} strokeDasharray="3 5" vertical={false} />
          <XAxis
            dataKey="time"
            type="number"
            domain={["dataMin", "dataMax"]}
            scale="time"
            tickFormatter={(x) => new Date(x).toISOString().slice(11, 16)}
            stroke="#7f93a7"
            tickLine={false}
            minTickGap={44}
            fontSize={10}
          />
          <YAxis
            stroke="#7f93a7"
            tickLine={false}
            axisLine={false}
            width={70}
            fontSize={10}
            domain={kind === "equity" ? ["auto", "auto"] : undefined}
            tickFormatter={(x) => Number(x).toLocaleString("en-CA", { maximumFractionDigits: 2 })}
          />
          <Tooltip
            labelFormatter={(x) => utcLabel(new Date(Number(x)).toISOString())}
            formatter={(value, name, item) => {
              const key = String(item.dataKey).replace(/_plot$/, "");
              return [
                `${text(item.payload[key])} ${kind === "drawdown" ? "%" : "CAD"}`,
                labels[key] || String(name),
              ];
            }}
            contentStyle={{
              background: "#111c28",
              border: "1px solid #2e4355",
              borderRadius: 8,
              fontSize: 11,
            }}
          />
          <Legend iconType="plainline" wrapperStyle={{ fontSize: 11, paddingTop: 14 }} />
          {keys.map((key, i) => (
            <Line
              key={key}
              dataKey={`${key}_plot`}
              name={labels[key]}
              stroke={colors[i]}
              strokeWidth={i === 0 ? 2 : 1.5}
              strokeDasharray={i === 1 ? "4 4" : undefined}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}