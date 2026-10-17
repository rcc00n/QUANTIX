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
type Saved = {
  id: string | null;
  version: number;
  effective_at: string | null;
  settings: TerminalSettings;
  defaults: TerminalSettings;
  delivery_active: boolean;
  delivery_stage: number;
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
function Toggle({
  label,
  value,
  onChange,
  help,
  disabled = false,
}: {
  label: string;
  value: boolean;
  onChange: (value: boolean) => void;
  help?: string;
  disabled?: boolean;
}) {
  return (
    <label className="terminal-toggle">
      <input
        type="checkbox"
        aria-label={label}
        checked={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>
        <strong>{label}</strong>
        {help && <small>{help}</small>}
      </span>
    </label>
  );
}
export function TerminalSettingsWorkspace() {
  const saved = useLive<Saved>("/settings/terminal", 60000);
  const health = useLive<Record<string, unknown>>("/terminal/health", 60000);
  const [notificationStatus, setNotificationStatus] = useState<NotificationStatus | null>(null);
  const [notificationRevision, setNotificationRevision] = useState(0);
  const [settings, setSettings] = useState<TerminalSettings | null>(null);
  const [version, setVersion] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (saved.data && !dirty && saved.data.version >= version) {
      setSettings(saved.data.settings);
      setVersion(saved.data.version);
    }
  }, [saved.data, dirty, version]);
  useEffect(() => {
    if (settings && location.hash === "#notification-policy") {
      document.getElementById("notification-policy")?.scrollIntoView({ block: "start" });
    }
  }, [!!settings]);
  function field(group: "market" | "notifications", key: string, value: unknown) {
    setSettings((s) => (s ? { ...s, [group]: { ...s[group], [key]: value } } : s));
    setDirty(true);
    setMessage("");
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!settings) return;
    setBusy(true);
    setError("");
    try {
      const result = await terminalFetch<Saved>("/settings/terminal", undefined, {
        method: "PUT",
        body: JSON.stringify({ settings, expected_version: version }),
      });
      setSettings(result.settings);
      setVersion(result.version);
      setMessage(
        `Settings version ${result.version} saved. Notification delivery follows pairing, worker health and this policy.`,
      );
      setDirty(false);
      saved.refresh();
      health.refresh();
      setNotificationRevision((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Settings could not be saved");
    } finally {
      setBusy(false);
    }
  }
  if (!settings)
    return (
      <section className="terminal-panel">
        <SectionTitle title="Market and notification preferences" />
        {saved.error ? (
          <Warning>{saved.error}</Warning>
        ) : (
          <p className="terminal-description">Loading persistent preferences…</p>
        )}
      </section>
    );
  const market = settings.market;
  const notifications = settings.notifications;
  const collector = asRecord(health.data?.collector);
  return (
    <>
      <TelegramConnection
        compact
        onStatus={setNotificationStatus}
        refreshKey={notificationRevision}
      />
      <section className="terminal-panel terminal-settings">
        <SectionTitle
          eyebrow="PERSISTENT TERMINAL PREFERENCES"
          title="Market coverage and alert policy"
        >
          <Tag value={`Version ${version}`} />
        </SectionTitle>
        <div className="settings-version-line">
          <span>Saved version {version}</span>
          <span>
            Collector applied version {text(collector.applied_settings_version, "unavailable")}
          </span>
          <span>Application occurs on the next bounded allocation cycle.</span>
        </div>
        {error && <Warning>{error}</Warning>}
        {message && (
          <div role="status" className="terminal-success">
            {message}
          </div>
        )}
        <form onSubmit={save}>
          <fieldset>
            <legend>Market coverage</legend>
            <p className="terminal-description">
              Public data availability, source quality and Canadian trading eligibility remain
              independent. Empty exchange or asset allowlists include all supported entries; they do
              not pause collection.
            </p>
            <div className="paper-form-grid">
              {(
                [
                  ["enabled_exchanges", "Enabled exchange slugs", "kraken, ndax"],
                  ["enabled_assets", "Enabled base assets", "BTC, ETH, SOL"],
                  [
                    "preferred_quote_currencies",
                    "Preferred quote currencies",
                    "CAD, USD, USDC, USDT",
                  ],
                ] as const
              ).map(([key, label, placeholder]) => (
                <label key={key}>
                  {label}
                  <input
                    aria-label={label}
                    value={Array.isArray(market[key]) ? (market[key] as string[]).join(", ") : ""}
                    placeholder={placeholder}
                    onChange={(e) =>
                      field(
                        "market",
                        key,
                        e.target.value
                          .split(",")
                          .map((x) => x.trim())
                          .filter(Boolean),
                      )
                    }
                  />
                </label>
              ))}
              {numericMarket.map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    aria-label={label}
                    type="number"
                    min={1}
                    max={key === "maximum_age_seconds" ? 60 : 50}
                    step={1}
                    value={text(market[key], "")}
                    onChange={(e) => field("market", key, e.target.valueAsNumber)}
                    required
                  />
                </label>
              ))}
              <label>
                Minimum native quote volume
                <input
                  aria-label="Minimum native quote volume"
                  inputMode="decimal"
                  value={text(market.minimum_liquidity, "")}
                  onChange={(e) => field("market", "minimum_liquidity", e.target.value)}
                  required
                />
              </label>
            </div>
            <div className="terminal-toggle-grid">
              <Toggle
                label="Prioritize CAD coverage"
                value={market.cad_focused === true}
                onChange={(v) => field("market", "cad_focused", v)}
              />
              <Toggle
                label="Include research anomalies"
                value={market.research_anomaly_scanning === true}
                onChange={(v) => field("market", "research_anomaly_scanning", v)}
                help="Preserve unusual evidence; never approves actionability."
              />
            </div>
            <p className="terminal-footnote">
              Volume is denominated in each market's native quote currency, not CAD. Source
              freshness may tighten validation; a saved preference never bypasses clock, fee or
              eligibility gates. USD, USDC and USDT remain distinct.
            </p>
          </fieldset>
          <fieldset id="notification-policy">
            <legend>Notification policy</legend>
            <div className="terminal-inactive">
              <Bell size={18} />
              <div>
                <strong>
                  {notificationStatus?.delivery_active
                    ? "Telegram delivery active"
                    : notificationStatus?.pairing?.paused
                      ? "Telegram delivery paused"
                      : "Telegram delivery inactive"}
                </strong>
                <p>
                  {notificationStatus?.paired
                    ? "Paired owner channel."
                    : "Securely pair the owner account above."}{" "}
                  Delivery requires enabled policy and a healthy worker. Paid push and voice remain
                  disabled.
                </p>
              </div>
            </div>
            <div className="terminal-toggle-grid">
              <Toggle
                label="Enable Telegram notifications"
                value={notifications.telegram_enabled === true}
                onChange={(v) => field("notifications", "telegram_enabled", v)}
              />
              <Toggle
                label="Ordinary actionable alerts"
                value={notifications.normal_alerts === true}
                onChange={(v) => field("notifications", "normal_alerts", v)}
              />
              <Toggle
                label="Ordinary research notifications"
                value={notifications.research_enabled === true}
                onChange={(v) => field("notifications", "research_enabled", v)}
                help="RESEARCH ONLY. Independent opt-in; never urgent or nighttime escalation."
              />
              <Toggle
                label="Emergency alert policy"
                value={notifications.emergency_alerts === true}
                onChange={(v) => field("notifications", "emergency_alerts", v)}
              />
              <Toggle
                label="Quiet hours"
                value={notifications.quiet_hours_enabled === true}
                onChange={(v) => field("notifications", "quiet_hours_enabled", v)}
              />
            </div>
            <p className="terminal-description">
              Actionable alerts must pass every minimum, exact executable depth, venue rules,
              verified fees and FX, funding, freshness, confidence and trading eligibility. A
              profitable research observation remains research.
            </p>
            <div className="paper-form-grid">
              <BuyCapitalControl
                value={text(notifications.minimum_buy_capital_cad, "50")}
                onChange={(value) => field("notifications", "minimum_buy_capital_cad", value)}
              />
              <BuyCapitalControl
                label="Night emergency Minimum Buy Capital · CAD"
                value={text(notifications.nighttime_minimum_buy_capital_cad, "250")}
                onChange={(value) =>
                  field("notifications", "nighttime_minimum_buy_capital_cad", value)
                }
              />
              {alertAmounts.map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    aria-label={label}
                    inputMode="decimal"
                    value={text(notifications[key], "")}
                    onChange={(e) => field("notifications", key, e.target.value)}
                    required
                  />
                </label>
              ))}
              <label>
                Quiet-hours ordinary delivery
                <select
                  aria-label="Quiet-hours ordinary delivery"
                  value={text(notifications.quiet_delivery, "SUPPRESS")}
                  onChange={(e) => field("notifications", "quiet_delivery", e.target.value)}
                >
                  <option value="SUPPRESS">Suppress ordinary notifications</option>
                  <option value="SILENT">Deliver silently</option>
                </select>
              </label>
              <label>
                Minimum data confidence
                <input readOnly value="HEALTHY · daytime and nighttime" />
              </label>
              <label>
                Reporting / quiet-hours timezone
                <input readOnly value="America/Edmonton" />
              </label>
              <label>
                Quiet hours start
                <input
                  aria-label="Quiet hours start"
                  type="time"
                  value={text(notifications.quiet_start, "")}
                  onChange={(e) => field("notifications", "quiet_start", e.target.value)}
                  required
                />
              </label>
              <label>
                Quiet hours end
                <input
                  aria-label="Quiet hours end"
                  type="time"
                  value={text(notifications.quiet_end, "")}
                  onChange={(e) => field("notifications", "quiet_end", e.target.value)}
                  required
                />
              </label>
              {alertTimes.map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    aria-label={label}
                    type="number"
                    min={0}
                    step={1}
                    value={text(notifications[key], "")}
                    onChange={(e) => field("notifications", key, e.target.valueAsNumber)}
                    required
                  />
                </label>
              ))}
            </div>
            <div className="terminal-toggle-grid">
              <Toggle
                label="Repeat urgent notifications until acknowledgment"
                value={notifications.repeat_until_acknowledged === true}
                onChange={(v) => field("notifications", "repeat_until_acknowledged", v)}
              />
              <Toggle
                label="Manual acknowledgment required"
                disabled
                value={notifications.require_manual_acknowledgment === true}
                onChange={(v) => field("notifications", "require_manual_acknowledgment", v)}
              />
              <Toggle
                label="Cancel when opportunity expires"
                disabled
                value={notifications.cancel_on_expiry === true}
                onChange={(v) => field("notifications", "cancel_on_expiry", v)}
              />
              <Toggle
                label="Paid emergency push · disabled"
                value={false}
                disabled
                onChange={(v) => field("notifications", "push_requested", v)}
              />
              <Toggle
                label="Voice calls · disabled"
                value={false}
                disabled
                onChange={(v) => field("notifications", "voice_requested", v)}
                help="Separate provider configuration and explicit cost authorization required. Telegram cannot place phone calls."
              />
            </div>
            <p className="terminal-footnote">
              Minimum Buy Capital uses the actual purchase cost, independently of profit, return,
              maximum budget and portfolio equity. Night thresholds cannot be weaker than daytime.
              Fresh confirmation and observed persistence are required for urgent escalation.
              Telegram cannot guarantee Focus bypass or waking the owner.
            </p>
          </fieldset>
          <fieldset>
            <legend>Owner reports · America/Edmonton</legend>
            <div className="terminal-toggle-grid">
              <Toggle
                label="Daily owner report"
                value={notifications.daily_report_enabled !== false}
                onChange={(value) => field("notifications", "daily_report_enabled", value)}
                help="Recorded opportunities, delivery, simulations, P&L, equity, benchmark and system issues."
              />
              <Toggle
                label="Weekly owner summary"
                value={notifications.weekly_report_enabled === true}
                onChange={(value) => field("notifications", "weekly_report_enabled", value)}
              />
            </div>
            <div className="paper-form-grid">
              <label>
                Report time · Edmonton
                <input
                  aria-label="Report time Edmonton"
                  type="time"
                  value={text(notifications.daily_report_time, "09:00")}
                  onChange={(e) => field("notifications", "daily_report_time", e.target.value)}
                  required
                />
              </label>
              <label>
                Weekly report day
                <select
                  aria-label="Weekly report day"
                  value={text(notifications.weekly_report_day, "0")}
                  onChange={(e) =>
                    field("notifications", "weekly_report_day", Number(e.target.value))
                  }
                >
                  {[
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                    "Sunday",
                  ].map((day, index) => (
                    <option key={day} value={index}>
                      {day}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </fieldset>
          <div className="paper-actions">
            <Button type="submit" disabled={busy || !dirty}>
              <Save size={14} />
              {busy ? "Saving…" : "Save terminal preferences"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || !saved.data?.defaults}
              onClick={() => {
                if (saved.data) {
                  setSettings(saved.data.defaults);
                  setDirty(true);
                  setMessage("Defaults loaded. Save to create a new settings version.");
                }
              }}
            >
              <RotateCcw size={14} /> Reset to defaults
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setDirty(false);
                setVersion(0);
                saved.refresh();
                setMessage("Reloading saved preferences; unsaved edits discarded.");
              }}
            >
              Discard edits and reload
            </Button>
            {dirty && <span className="terminal-dirty">Unsaved changes</span>}
          </div>
        </form>
        <JsonDetails
          title="Actual saved preference version and application state"
          value={{
            version,
            effective_at: utcLabel(saved.data?.effective_at),
            applied_market_preferences: collector.applied_market_preferences,
            delivery_active: notificationStatus?.delivery_active ?? null,
          }}
        />
      </section>
    </>
  );
}
