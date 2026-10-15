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