"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Bell,
  CirclePause,
  ExternalLink,
  Link2,
  Play,
  Send,
  ShieldCheck,
  Unlink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, LiveStatus, SectionTitle, Tag, Warning } from "@/components/terminal-ui";
import { terminalFetch, useLive } from "@/lib/terminal";
import { utcLabel } from "@/lib/paper";
import type {
  NotificationHistory,
  NotificationPreview,
  NotificationStatus,
} from "@/lib/notifications";

type Challenge = {
  challenge_id: string;
  challenge: string;
  start_url: string | null;
  expires_at: string;
};