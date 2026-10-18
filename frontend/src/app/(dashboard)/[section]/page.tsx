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
export default async function SectionPage({ params }: { params: Promise<{ section: string }> }) {
  await requireUser();
  const { section } = await params;
  const item = navigation.find((x) => x.slug === section);
  if (!item) notFound();
  const statusPromise = getStatus();
  let feeds: ExchangeFeed[] = [];
  if (section === "exchanges") {
    try {
      const response = await backendFetch("/exchanges");
      if (response.ok) feeds = await response.json();
    } catch {
      /* Explicit unavailable state in explorer. */
    }
  }
  const status = await statusPromise;
  const subtitles: Record<string, string> = {
    overview: "Live market intelligence and isolated virtual portfolio performance.",
    opportunities: "Executable-depth projections, current evidence and confidence gates.",
    exchanges: "Public market feeds, discovery and source confidence.",
    "paper-trading": "Independent simulated legs, real observations and auditable outcomes.",
    portfolio: "Virtual inventory, cost basis and no-trading benchmark.",
    analytics: "Reconciled performance, recorded history and self-contained exports.",
    settings: "Versioned strategies, market coverage and owner notification policies.",
    "system-health": "Live operational measurements, clock confidence and data reliability.",
    signals: "Private owner pairing, quantitative alert policy and recorded delivery outcomes.",
    "ai-insights": "Optional explanatory analysis. Core decisions remain quantitative.",
  };
  return (
    <>
      <Header
        title={section === "overview" ? "Command Center" : item.label}
        subtitle={subtitles[section]}
        status={status}
      />
      {section === "overview" ? (
        <CommandCenter />
      ) : section === "opportunities" ? (
        <OpportunityRadar />
      ) : section === "exchanges" ? (
        <ExchangeExplorer initialFeeds={feeds} />
      ) : section === "paper-trading" ? (
        <PaperWorkspace view="trading" />
      ) : section === "portfolio" ? (
        <PaperWorkspace view="portfolio" />
      ) : section === "analytics" ? (
        <AnalyticsWorkspace />
      ) : section === "settings" ? (
        <>
          <PaperWorkspace view="settings" />
          <TerminalSettingsWorkspace />
        </>
      ) : section === "system-health" ? (
        <SystemHealthWorkspace />
      ) : section === "signals" ? (
        <SignalsWorkspace />
      ) : (
        <AiInsightsWorkspace />
      )}
    </>
  );
}
