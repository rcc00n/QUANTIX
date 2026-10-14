"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import {
  Activity,
  ArrowLeftRight,
  Bell,
  ChartNoAxesCombined,
  CircleHelp,
  Layers,
  LayoutDashboard,
  LogOut,
  Menu,
  Network,
  Radar,
  Settings2,
  Sparkles,
  Wallet,
  X,
} from "lucide-react";
import { navigation } from "@/lib/navigation";
import { Brand } from "@/components/brand";
import { Button } from "@/components/ui/button";
const icons = {
  overview: LayoutDashboard,
  radar: Radar,
  exchanges: Network,
  trading: ArrowLeftRight,
  portfolio: Wallet,
  analytics: ChartNoAxesCombined,
  signals: Bell,
  ai: Sparkles,
  settings: Settings2,
  health: Activity,
};
export function Sidebar() {
  const path = usePathname();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="mobile-toggle"
        aria-label={open ? "Close navigation" : "Open navigation"}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? <X /> : <Menu />}
      </button>
      {open && (
        <button
          className="nav-scrim"
          aria-label="Close navigation"
          onClick={() => setOpen(false)}
        />
      )}
      <aside className={`sidebar ${open ? "is-open" : ""}`}>
        <Link href="/overview" aria-label="QUANTIX overview" onClick={() => setOpen(false)}>
          <Brand />
        </Link>
        <div className="workspace-label">
          WORKSPACE <span>01</span>
        </div>
        <nav aria-label="Main navigation">
          {navigation.map((item, index) => {
            const Icon = icons[item.icon];
            const active = path === `/${item.slug}`;
            return (
              <Link
                key={item.slug}
                href={`/${item.slug}`}
                aria-current={active ? "page" : undefined}
                className={`nav-item ${active ? "active" : ""} ${index === 8 ? "nav-divider" : ""}`}
                onClick={() => setOpen(false)}
              >
                <Icon size={18} />
                <span>{item.label}</span>
                {item.slug === "ai-insights" && <span className="nav-badge">OPT</span>}
              </Link>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <div className="paper-lock">
            <Layers size={17} />
            <span>
              Paper environment<small>Real orders disabled</small>
            </span>
            <span className="status-dot" />
          </div>
          <p>
            <CircleHelp size={14} /> Market intelligence · v0.7.1
          </p>
        </div>
      </aside>
    </>
  );
}