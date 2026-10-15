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