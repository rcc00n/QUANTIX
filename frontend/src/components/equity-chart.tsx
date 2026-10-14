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