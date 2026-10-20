import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createHash } from "node:crypto";

// Explicitly SYNTHETIC browser-only data; never written to the application DB.
const primary = "aaaaaaaa-0000-4000-8000-000000000005",
  research = "bbbbbbbb-0000-4000-8000-000000000005",
  proposal = "cccccccc-0000-4000-8000-000000000005";
const strategy = {
  preset: "Balanced",
  starting_capital_cad: "10000",
  trade_amount_cad: "1000",
  minimum_profit_cad: "5",
  minimum_profit_pct: "0.5",
  threshold_logic: "AND",
  reaction_delay_seconds: 30,
  maximum_concurrent: 3,
  mode: "PRE_FUNDED",
  allowed_exchanges: [],
  allowed_assets: [],
};
const sessions = [
  {
    id: primary,
    name: "SYNTHETIC validated",
    classification: "VALIDATED",
    status: "ACTIVE",
    mode: "PRE_FUNDED",
    initial_equity_cad: "10000",
    strategy_version_id: primary,
    settings: strategy,
  },
  {
    id: research,
    name: "SYNTHETIC research",
    classification: "RESEARCH",
    status: "ACTIVE",
    mode: "RESEARCH_SHADOW",
    initial_equity_cad: "10000",
    strategy_version_id: research,
    settings: { ...strategy, mode: "RESEARCH_SHADOW", preset: "Aggressive Research" },
  },
];
const metrics = {
  counts: { MISSED: 2 },
  completed_simulations: 0,
  missed_simulations: 2,
  partially_completed: 0,
  failed_simulations: 0,
  expired_simulations: 0,
  scheduled_simulations: 0,
  win_rate_pct: null,
  completed_with_available_cad_pnl: 0,
  maximum_drawdown_pct: "1.5",
  by_route: [],
  by_asset: [],
  by_strategy: [],
  by_reaction_delay_ms: [],
  failure_reasons: { SYNTHETIC_INVENTORY_MISSING: 2 },
};
function portfolio(r = false) {
  return {
    initial_equity_cad: "10000",
    equity_cad: r ? "9850.123456789012345678" : "10000",
    net_simulated_pnl_cad: r ? "-149.876543210987654322" : "0",
    realized_trading_pnl_cad: "0",
    realized_fx_pnl_cad: "0",
    unrealized_pnl_cad: r ? "-149.876543210987654322" : "0",
    fees_cad: "0",
    expenses_cad: "0",
    portfolio_return_pct: r ? "-1.498765432109876543" : "0",
    no_trading_benchmark_cad: r ? "9850.123456789012345678" : "10000",
    strategy_excess_cad: "0",
    capital_utilization_pct: "0",
    available_capital_cad: "10000",
    drawdown_pct: "0",
    reconciliation_delta_cad: "0",
    as_of: "2026-01-02T00:00:00Z",
    limitations: [],
    balances: [],
  };
}
const preferences = {
  schema_version: "terminal-v1",
  market: {
    enabled_exchanges: [],
    enabled_assets: [],
    preferred_quote_currencies: ["CAD", "USD", "USDC", "USDT"],
    cad_focused: true,
    research_anomaly_scanning: true,
    maximum_age_seconds: 30,
    minimum_liquidity: "0",
    spot_only: true,
    maximum_markets_per_exchange: 10,
  },
  notifications: {
    telegram_enabled: false,
    minimum_buy_capital_cad: "50",
    nighttime_minimum_buy_capital_cad: "250",
    minimum_liquidity_cad: "50",
    nighttime_minimum_liquidity_cad: "500",
    maximum_quote_age_seconds: 10,
    nighttime_maximum_quote_age_seconds: 3,
    minimum_persistence_seconds: 0,
    nighttime_minimum_persistence_seconds: 10,
    daily_emergency_cap: 5,
    research_enabled: false,
    quiet_delivery: "SUPPRESS",
    daily_report_enabled: true,
    daily_report_time: "09:00",
    weekly_report_enabled: false,
    weekly_report_day: 0,
    minimum_profit_cad: "5",
    minimum_return_pct: "0.5",
    normal_alerts: true,
    emergency_alerts: false,
    timezone: "America/Edmonton",
    quiet_hours_enabled: true,
    quiet_start: "23:00",
    quiet_end: "07:00",
    nighttime_minimum_profit_cad: "25",
    nighttime_minimum_return_pct: "1",
    cooldown_seconds: 300,
    repeat_until_acknowledged: false,
    repeat_interval_seconds: 60,
    maximum_repeats: 5,
    escalation_cooldown_seconds: 600,
    push_requested: false,
    voice_requested: false,
    cancel_on_expiry: true,
    require_manual_acknowledgment: true,
  },
};
async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill("quantix");
  await page
    .getByLabel("Password", { exact: true })
    .fill(readFileSync(resolve(__dirname, "../../.secrets/admin-password"), "utf8").trim());
  await page.getByRole("button", { name: "Enter workspace" }).click();
  await expect(page).toHaveURL(/\/overview$/);
}
async function fixtures(page: Page) {
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  let version = 1;
  let saved = structuredClone(preferences);
  await page.route("**/api/notifications/status", (route) =>
    route.fulfill({
      json: {
        paired: false,
        pairing: null,
        pending_challenges: [],
        worker: null,
        telegram_enabled: false,
        delivery_active: false,
        providers: {
          telegram: { available: false },
          push: { enabled: false, reason: "DISABLED" },
          voice: { enabled: false, reason: "DISABLED" },
        },
        queue_counts: {},
      },
    }),
  );
  await page.route("**/api/terminal/**", (route) =>
    route.fulfill({
      json: {
        connected_exchanges: 2,
        venue_groups: 2,
        monitored_markets: 4,
        compatible_intersections: 2,
        scanner: {
          healthy: true,
          clock: { trusted: true, host_healthy: false },
          metrics: { verified_market: 0, research_only: 1 },
        },
        paper: { healthy: true },
        feeds: [],
        events: [],
        warnings: [],
        recent_opportunities: [],
        recent_simulations: [],
        collector: { applied_settings_version: 1 },
        telemetry: {},
      },
    }),
  );
  await page.route("**/api/settings/terminal", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      writes.push({ path: "terminal", body });
      saved = body.settings;
      version++;
    }
    await route.fulfill({
      json: {
        id: primary,
        version,
        settings: saved,
        defaults: preferences,
        effective_at: "2026-01-02T00:00:00Z",
        delivery_active: false,
        delivery_stage: 6,
      },
    });
  });
  await page.route("**/api/strategies", (route) =>
    route.fulfill({
      json: {
        current: { id: primary, version: 1, settings: strategy },
        presets: {
          Balanced: strategy,
          Conservative: strategy,
          "Aggressive Research": sessions[1].settings,
        },
        reaction_delays: [5, 15, 30, 60, 120],
      },
    }),
  );
  await page.route("**/api/analytics/**", (route) => {
    const u = new URL(route.request().url()),
      r = u.pathname.includes(research);
    return route.fulfill({
      json: u.pathname.endsWith("/series")
        ? {
            available_count: 2,
            sampling: "SYNTHETIC_BROWSER_FIXTURE",
            items: [
              { ...portfolio(false), as_of: "2026-01-01T00:00:00Z" },
              { ...portfolio(r), as_of: "2026-01-02T00:00:00Z" },
            ],
          }
        : {
            schema_version: "analytics-v1",
            classification: r ? "RESEARCH" : "VALIDATED",
            period: { timezone: "America/Edmonton" },
            metrics,
            portfolio: portfolio(r),
            by_trade_size: [],
            strategy_versions: [{ id: r ? research : primary, version: 1 }],
            reconciliation: { delta_cad: "0" },
            limitations: [],
          },
    });
  });
  await page.route("**/api/paper/**", async (route) => {
    const u = new URL(route.request().url());
    if (route.request().method() === "POST")
      writes.push({ path: u.pathname, body: route.request().postDataJSON() });
    let data: unknown = { items: [], total: 0, limit: 25, offset: 0 };
    if (u.pathname.endsWith("/sessions")) data = { items: sessions, total: 2 };
    else if (u.pathname.endsWith("/portfolio")) data = portfolio(u.pathname.includes(research));
    else if (u.pathname.endsWith("/metrics")) data = metrics;
    else if (u.pathname.endsWith("/catalog")) data = { exchanges: [], assets: [] };
    else if (u.pathname.endsWith("/coverage"))
      data = {
        balances: [],
        routes: [
          {
            route_id: primary,
            asset: "SYNTH",
            buy_exchange: "synthetic_buy",
            sell_exchange: "synthetic_sell",
            inventory_sufficient: false,
            maximum_funded_acquisition_cad: null,
            projected_net_profit_cad: "-0.123456789012345678",
            funded_size_net_profit_cad: null,
            market_profitability: "NON_POSITIVE",
            actionable: false,
            required: [
              {
                exchange: "synthetic_sell",
                asset: "SYNTH",
                required_quantity: "3.123456789012345678",
                available_quantity: "0",
                missing_quantity: "3.123456789012345678",
              },
            ],
            reasons: ["SYNTHETIC_INVENTORY_MISSING"],
          },
        ],
        limitations: [],
      };
    else if (u.pathname.endsWith("/proposal"))
      data = {
        id: proposal,
        capital_cad: "10000",
        policy: "COVERAGE",
        total_cad: "10000",
        unallocated_cad: "0",
        route_coverage: { covered_routes: 1 },
        limitations: ["SYNTHETIC_BROWSER_FIXTURE"],
        expires_at: "2026-01-03T00:00:00Z",
        allocations: [
          {
            exchange: "synthetic",
            asset: "fiat:CAD",
            quantity: "5000",
            cad_rate: "1",
            cad_value: "5000",
          },
          {
            exchange: "synthetic",
            asset: "asset:SYNTH",
            quantity: "50",
            cad_rate: "100",
            cad_value: "5000",
          },
        ],
      };
    else if (u.pathname.endsWith("/experiments"))
      data = { ...sessions[1], id: proposal, reused: false };
    await route.fulfill({ json: data });
  });
  return writes;
}

test("SYNTHETIC: negative research analytics preserve exact values, zero win-rate samples and scoped export controls", async ({
  page,
}) => {
  await fixtures(page);
  await signIn(page);
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "Analytics", exact: true })
    .click();
  await expect(page.getByLabel("Analytics portfolio")).toHaveValue(primary);
  await page.getByLabel("Analytics portfolio").selectOption(research);
  await expect(page.getByText("Research-only experiment.", { exact: false })).toBeVisible();
  await expect(page.getByTitle("9850.123456789012345678 CAD").first()).toBeVisible();
  await expect(page.getByText("SYNTHETIC INVENTORY MISSING")).toBeVisible();
  await expect(
    page.getByRole("img", { name: "equity chart from recorded checkpoints" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Export Everything for ChatGPT", exact: true }),
  ).toBeVisible();
  await page.getByText("Advanced custom-filter export", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Export selected scope", exact: true }),
  ).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
});

async function openLiveAnalytics(page: Page) {
  await signIn(page);
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "Analytics", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Portfolio and no-trading benchmark" }),
  ).toBeVisible();
  const list = await (await page.request.get("/api/paper/sessions")).json();
  expect(list.items.length).toBeGreaterThan(0);
  const sessionId: string = list.items[0].id;
  const response = await page.request.get(`/api/analytics/sessions/${sessionId}`);
  expect(response.status()).toBe(200);
  const result = await response.json();
  if (result.metrics.completed_simulations === 0) expect(result.metrics.win_rate_pct).toBeNull();
  const end: string = result.period.end;
  const cutoff = Date.parse(end);
  expect(Number.isFinite(cutoff)).toBe(true);
  return { sessionId, cutoff, end };
}