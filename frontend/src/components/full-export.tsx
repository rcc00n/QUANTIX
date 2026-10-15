"use client";

import { useEffect, useRef, useState } from "react";
import { FileArchive, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Warning } from "@/components/terminal-ui";

type Job = {
  id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  scope: string;
  completed_datasets: number;
  total_datasets: number | null;
  dataset: string | null;
  dataset_rows: number;
  uncompressed_bytes: number;
  bytes?: number;
  file_count?: number;
  error: string | null;
  download_url: string | null;
  coverage_level?: string;
  cancel_requested?: boolean;
};