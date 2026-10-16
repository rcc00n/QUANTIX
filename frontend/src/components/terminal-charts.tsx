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