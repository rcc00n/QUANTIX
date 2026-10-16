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
export function AmountBars({
  rows,
  labelKey = "label",
  valueKey = "value",
  unit = "CAD",
  empty = "No completed CAD-valued results",
}: {
  rows: Record<string, unknown>[];
  labelKey?: string;
  valueKey?: string;
  unit?: string;
  empty?: string;
}) {
  const data = rows
    .map((x) => ({ ...x, label: text(x[labelKey]), value: plot(x[valueKey]), exact: x[valueKey] }))
    .filter((x) => x.value !== null);
  if (!data.length)
    return (
      <Empty title={empty}>
        Underlying records are unavailable or the reporting period contains no matching results.
      </Empty>
    );
  return (
    <div
      className="terminal-chart compact-chart"
      role="img"
      aria-label={`Recorded ${unit} distribution`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data.slice(0, 12)} margin={{ top: 10, right: 8, bottom: 12, left: 0 }}>
          <CartesianGrid stroke={stroke} vertical={false} />
          <XAxis dataKey="label" stroke="#7f93a7" fontSize={10} tickLine={false} interval={0} />
          <YAxis stroke="#7f93a7" fontSize={10} width={55} axisLine={false} tickLine={false} />
          <Tooltip
            formatter={(_v, _n, item) => [`${text(item.payload.exact)} ${unit}`, "Recorded result"]}
            contentStyle={{
              background: "#111c28",
              border: "1px solid #2e4355",
              borderRadius: 8,
              fontSize: 11,
            }}
          />
          <Bar dataKey="value" fill="#62cbdc" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {data.slice(0, 12).map((row, i) => (
              <Cell key={i} fill={row.value !== null && row.value < 0 ? "#ee97a3" : "#62cbdc"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}