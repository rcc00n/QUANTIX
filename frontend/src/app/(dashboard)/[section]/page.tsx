import { notFound } from "next/navigation";
import { ShieldCheck } from "lucide-react";
import { backendFetch, getStatus, requireUser, type SystemStatus } from "@/lib/api";
import { navigation } from "@/lib/navigation";
import { ExchangeExplorer } from "@/components/exchange-explorer";
import { OpportunityRadar } from "@/components/opportunity-radar";
import { PaperWorkspace } from "@/components/paper-workspace";
import { CommandCenter, SystemHealthWorkspace } from "@/components/command-center";
import { AnalyticsWorkspace } from "@/components/analytics-workspace";
import { TerminalSettingsWorkspace } from "@/components/terminal-settings";
import { SignalsWorkspace } from "@/components/signals-workspace";
import { AiInsightsWorkspace } from "@/components/ai-insights";
import type { ExchangeFeed } from "@/lib/market-data";
import { Refresh } from "@/components/refresh";

function Header({
  title,
  subtitle,
  status,
}: {
  title: string;
  subtitle: string;
  status: SystemStatus | null;
}) {
  return (
    <>
      <div className="page-header">
        <div>
          <div className="eyebrow">
            <span className="tiny-line" /> QUANTIX / {title.toUpperCase()}
          </div>
          <h1>
            {title}
            <span className="title-dot">.</span>
          </h1>
          <p>{subtitle}</p>
        </div>
        <Refresh />
      </div>
      <div className="environment-strip">
        <span>
          <ShieldCheck size={15} /> Paper trading only
        </span>
        <span>
          <i className={`status-dot ${status ? "" : "offline"}`} />
          {status ? "Backend connected" : "Backend unavailable"}
        </span>
        <span className="strip-last">
          Stage {String(status?.stage || 7).padStart(2, "0")} <span className="muted">/ 07</span>
        </span>
      </div>
    </>
  );
}