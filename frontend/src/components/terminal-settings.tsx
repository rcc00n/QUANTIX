"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Bell, RotateCcw, Save } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useLive, terminalFetch, asRecord, text } from "@/lib/terminal";
import { SectionTitle, Warning, Tag, JsonDetails } from "@/components/terminal-ui";
import { utcLabel } from "@/lib/paper";
import { BuyCapitalControl } from "@/components/buy-capital-control";
import { TelegramConnection } from "@/components/signals-workspace";
import type { NotificationStatus } from "@/lib/notifications";

type TerminalSettings = {
  schema_version: string;
  market: Record<string, unknown>;
  notifications: Record<string, unknown>;
};
const numericMarket = [
  ["maximum_age_seconds", "Maximum source age · seconds"],
  ["maximum_markets_per_exchange", "Market cap per exchange"],
] as const;
const alertAmounts = [
  ["minimum_profit_cad", "Normal minimum profit · CAD"],
  ["minimum_return_pct", "Normal minimum return · %"],
  ["nighttime_minimum_profit_cad", "Night emergency profit · CAD"],
  ["nighttime_minimum_return_pct", "Night emergency return · %"],
  ["minimum_liquidity_cad", "Normal minimum executable liquidity · CAD"],
  ["nighttime_minimum_liquidity_cad", "Night emergency executable liquidity · CAD"],
] as const;
const alertTimes = [
  ["cooldown_seconds", "Alert cooldown · seconds"],
  ["repeat_interval_seconds", "Urgent repeat interval · seconds"],
  ["maximum_repeats", "Maximum urgent repeats"],
  ["escalation_cooldown_seconds", "Escalation cooldown · seconds"],
  ["maximum_quote_age_seconds", "Normal maximum quote age · seconds"],
  ["nighttime_maximum_quote_age_seconds", "Night emergency maximum quote age · seconds"],
  ["minimum_persistence_seconds", "Normal minimum observed persistence · seconds"],
  ["nighttime_minimum_persistence_seconds", "Night emergency observed persistence · seconds"],
  ["daily_emergency_cap", "Daily emergency cap"],
] as const;