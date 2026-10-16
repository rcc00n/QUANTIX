"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Exact, Tag, Warning } from "@/components/terminal-ui";
import type { FundingCatalog } from "@/components/funding-details";
import { assetLabel, utcLabel } from "@/lib/paper";
import { terminalFetch } from "@/lib/terminal";

type RebalanceEstimate = {
  status: "NO_REBALANCE_REQUIRED" | "BLOCKED" | "RESEARCH_TRANSFER_ESTIMATE";
  decision_at: string;
  request: { from_exchange: string; to_exchange: string; asset: string; network: string | null };
  no_rebalance_benchmark: {
    source_available_quantity: string;
    destination_available_quantity: string;
    destination_deficit: string;
    fee_quantity: string;
  };
  hypothetical_transfer: {
    estimated_available_at: string;
    source_debit_quantity: string;
    fee_quantity: string;
    delivered_quantity_after_settlement: string;
    remaining_destination_deficit: string;
    transfer_fee_cad: string | null;
    conversion_cost_cad: string | null;
  } | null;
  blockers: string[];
  risks: string[];
};