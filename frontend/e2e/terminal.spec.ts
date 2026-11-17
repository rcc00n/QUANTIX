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
            maximum_observed_acquisition_cad: "123.765432109876543210",
            maximum_affordable_acquisition_cad: null,
            funding_model: "PRE_FUNDED",
            projected_net_profit_cad: "-0.123456789012345678",
            funded_size_net_profit_cad: null,
            market_profitability: "NON_POSITIVE",
            actionable: false,
            required: [
              {
                side: "SELL",
                exchange: "synthetic_sell",
                asset: "SYNTH",
                required_quantity: "3.123456789012345678",
                available_quantity: "0",
                reserved_quantity: "1.999999999999999999",
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

test("SYNTHETIC: planner explains exact missing inventory and creates independent funding", async ({
  page,
}) => {
  const writes = await fixtures(page);
  await signIn(page);
  await page.getByRole("link", { name: "Portfolio", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Portfolio accounting" })).toBeVisible();
  const equity = page.locator(".paper-summary > div").filter({ hasText: "Current virtual equity" });
  await expect(equity.getByTitle("10000 CAD")).toBeVisible();
  await page.route(`**/api/paper/sessions/${research}/portfolio`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 700));
    await route.fulfill({ json: portfolio(true) });
  });
  await page.getByLabel("Paper portfolio").selectOption(research);
  await expect(equity.getByText("Unavailable", { exact: true })).toBeVisible();
  await expect(equity.getByTitle("9850.123456789012345678 CAD")).toBeVisible();
  await page.getByRole("button", { name: "Virtual Funding Planner", exact: true }).click();
  const planner = page.getByRole("region", { name: "Virtual Funding Planner" });
  await expect(planner).toBeVisible();
  await planner
    .locator("summary")
    .filter({ hasText: "Current virtual funds and route constraints" })
    .click();
  await planner.locator(".funding-route > summary").first().click();
  await expect(
    planner.locator(".funding-requirement strong").getByTitle("3.123456789012345678"),
  ).toBeVisible();
  await expect(planner.getByText("MISSING INVENTORY", { exact: true })).toBeVisible();
  await expect(planner.getByTitle("1.999999999999999999")).toBeVisible();
  await expect(planner.getByTitle("123.765432109876543210 CAD")).toBeVisible();
  await expect(planner.getByText("Non-positive market projection", { exact: true })).toBeVisible();
  await expect(planner.getByTitle("-0.123456789012345678 CAD")).toBeVisible();
  await page.getByRole("button", { name: "Propose virtual funding" }).click();
  await expect(page.getByRole("table", { name: "Proposed funding allocations" })).toBeVisible();
  await page.getByRole("button", { name: "Create independent experiment" }).click();
  await expect(page.getByText(/Created independent research experiment/)).toBeVisible();
  const create = writes.find((x) => x.path.endsWith("/experiments"));
  expect(create?.body.proposal_id).toBe(proposal);
  expect(typeof create?.body.idempotency_key).toBe("string");
  expect(create?.body.capital_cad).toBe("10000");
  await expect(planner.getByText("Portfolio created from this proposal.")).toBeVisible();
  await expect(planner.getByRole("button", { name: "Create independent experiment" })).toHaveCount(
    0,
  );
  expect(writes.some((x) => x.path.includes("/rebalance"))).toBe(false);
  await page.getByRole("button", { name: "New portfolio", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Classification", exact: true })
    .selectOption("VALIDATED");
  await page.getByText("Set initial CAD inventories", { exact: true }).click();
  await page.getByRole("button", { name: "Add allocation", exact: true }).click();
  const rate = page.getByLabel("Allocation 1 CAD rate", { exact: true });
  await expect(rate).toHaveValue("1");
  await expect(rate).toHaveAttribute("readonly", "");
  await page
    .getByRole("combobox", { name: "Classification", exact: true })
    .selectOption("RESEARCH");
  await page.getByLabel("Portfolio name", { exact: true }).fill("SYNTHETIC prefunded run");
  await page.getByLabel("Starting equity · CAD", { exact: true }).fill("50000.000000000000000001");
  await page.getByRole("button", { name: "Open funding planner", exact: true }).click();
  await expect(planner.getByLabel("Funding capital CAD", { exact: true })).toHaveValue(
    "50000.000000000000000001",
  );
  expect(writes.some((item) => item.path.endsWith("/sessions"))).toBe(false);
});

test("SYNTHETIC: funding modes share a cutoff and creation retries the same immutable proposal", async ({
  page,
}) => {
  await fixtures(page);
  const comparisonTime = "2026-01-02T11:00:00Z";
  const capital = "10000.123456789012345678";
  const modes = ["COVERAGE", "LIQUIDITY_WEIGHTED", "ROUTE_OPTIMIZED", "DIVERSIFIED_HYBRID"];
  const plans = modes.map((policy, i) => ({
    id: `cccccccc-0000-4000-8000-00000000000${i + 1}`,
    policy,
    capital_cad: capital,
    eligibility: "RESEARCH",
    total_cad: capital,
    as_of: comparisonTime,
    expires_at: "2026-01-02T11:05:00Z",
    trade_amount_cad: "500",
    unallocated_cad: "0",
    limits: {},
    limitations: [],
    reasons: i === 1 ? ["MAXIMUM_RESERVE_EXCEEDED"] : [],
    feasible: i !== 1,
    creatable: i !== 0 && i !== 1,
    funded_route_count: 4 + i,
    simultaneous_funded_route_count: 2,
    median_funded_acquisition_cad: "505.999999999999999999",
    route_coverage: { considered_routes: 10 },
    evidence: [{ evaluation_id: "synthetic-evaluation-at-cutoff" }],
    requirements: [
      {
        route_id: "synthetic-funded-route",
        asset: "SYNTH",
        buy_exchange: "synthetic_buy",
        sell_exchange: "synthetic_sell",
        required: [],
        funded: true,
      },
    ],
    route_capacity: [
      {
        route_id: "synthetic-funded-route",
        maximum_funded_acquisition_cad: "505.999999999999999999",
        maximum_observed_acquisition_cad: "505.999999999999999999",
        projected_net_profit_cad: "-1",
        target_pinned: {
          acquisition_cad: "500",
          net_profit_cad: "-0.5",
          deficits: [],
          projected_costs: {
            fees_cad: "2.123456789012345678",
            fx_drag_cad: "0.123456789012345678",
            slippage_reserve_cad: "1.050000000000000001",
            rebalancing_cad: null,
          },
        },
        limiting_constraints: ["NON_POSITIVE_NET_PROJECTION"],
        fundable_with_reallocation: false,
        actionable: false,
      },
    ],
    allocation_summary: {
      allocated_cad: capital,
      invested_cad: "9000.123456789012345678",
      reserve_cad: "1000",
      reserve_pct: "9.9998765",
      stablecoin_cad: "675",
      stablecoin_pct: "6.749917",
      funded_venues: 2,
      funded_cells: 3,
      by_asset: {
        "asset:SYNTH": { cad_value: "9000.123456789012345678", share_pct: "90.0001235" },
      },
      by_exchange: { synthetic_sell: { cad_value: "4000", share_pct: "39.9995" } },
    },
    coverage_by_size: [
      {
        trade_amount_cad: "500",
        funded_alternative_routes: 7,
        simultaneous_funded_routes: 2,
        profitable_funded_routes: 0,
        considered_routes: 10,
      },
    ],
    allocations: [
      {
        exchange: "synthetic_sell",
        asset: "asset:SYNTH",
        quantity: "3.123456789012345678",
        cad_rate: "277.500000000000000001",
        cad_value: "9000.123456789012345678",
      },
    ],
  }));
  const compareWrites: unknown[] = [];
  const creates: Record<string, unknown>[] = [];
  await page.route("**/api/paper/funding/compare", async (route) => {
    compareWrites.push(route.request().postDataJSON());
    await route.fulfill({ json: { as_of: comparisonTime, proposals: plans } });
  });
  await page.route("**/api/paper/funding/experiments", async (route) => {
    creates.push(route.request().postDataJSON());
    if (creates.length === 1)
      await route.fulfill({ status: 503, json: { detail: "SYNTHETIC retryable reply" } });
    else await route.fulfill({ json: { id: research, reused: true } });
  });
  await signIn(page);
  await page.getByRole("link", { name: "Portfolio", exact: true }).click();
  await page.getByRole("button", { name: "Virtual Funding Planner", exact: true }).click();
  const planner = page.getByRole("region", { name: "Virtual Funding Planner" });
  await planner.getByLabel("Funding capital CAD").fill(capital);
  await planner.getByLabel("Funding target trade budget CAD").fill("500");
  await planner.getByText("Limits, reserve and allowed markets", { exact: true }).click();
  await planner.getByLabel("Minimum position · CAD", { exact: true }).fill("60.000000000000000001");
  await planner.getByRole("button", { name: "Compare all modes", exact: true }).click();
  await expect(
    planner.getByText("Shared evidence cutoff 2026-01-02 11:00:00 UTC.", { exact: false }),
  ).toBeVisible();
  expect(compareWrites).toMatchObject([
    {
      capital_cad: capital,
      trade_amount_cad: "500",
      min_cell_cad: "60.000000000000000001",
      eligibility: "RESEARCH",
    },
  ]);
  expect(compareWrites[0]).not.toHaveProperty("policy");
  await expect(
    planner.getByRole("table", { name: "Funding mode comparison" }).getByRole("row"),
  ).toHaveCount(5);
  await expect(planner.getByRole("button", { name: "Create independent experiment" })).toHaveCount(
    0,
  );
  await planner.getByRole("button", { name: "Baseline route coverage", exact: true }).click();
  await expect(
    planner.getByText("This study proposal cannot fund a new portfolio.", { exact: false }),
  ).toBeVisible();
  await expect(planner.getByRole("button", { name: "Create independent experiment" })).toHaveCount(
    0,
  );
  await planner.getByRole("button", { name: "Liquidity weighted", exact: true }).click();
  await expect(planner.getByText("MAXIMUM RESERVE EXCEEDED", { exact: true })).toBeVisible();
  await expect(planner.getByRole("button", { name: "Create independent experiment" })).toHaveCount(
    0,
  );
  await planner.getByRole("button", { name: "Diversified hybrid", exact: true }).click();
  const selected = planner.getByRole("region", { name: "Selected funding proposal" });
  await expect(
    selected
      .getByRole("table", { name: "Target trade sizes" })
      .getByRole("row")
      .nth(1)
      .getByRole("cell")
      .last(),
  ).toHaveText("0");
  await expect(
    selected
      .getByRole("table", { name: "Proposed funding allocations" })
      .getByTitle("277.500000000000000001"),
  ).toBeVisible();
  await selected
    .getByText("Both-leg requirements and funding constraints", { exact: true })
    .click();
  await selected.locator(".funding-route > summary").click();
  const costs = selected.getByRole("group", { name: "Expected route costs in CAD" });
  await expect(costs.getByText("Expected costs · Pinned market size")).toBeVisible();
  await expect(costs.getByTitle("2.123456789012345678 CAD")).toBeVisible();
  await expect(costs.getByTitle("1.050000000000000001 CAD")).toBeVisible();
  await expect(costs.getByText("FX drag (reflected in conversion)")).toBeVisible();
  await expect(costs.getByTitle("0.123456789012345678 CAD")).toBeVisible();
  await expect(costs.getByText("Unavailable", { exact: true })).toBeVisible();
  await selected.getByText("Inspect retained allocation evidence", { exact: true }).click();
  await expect(selected.getByText(/synthetic-evaluation-at-cutoff/)).toBeVisible();
  await planner.getByRole("button", { name: "Create independent experiment" }).click();
  await expect(planner.getByText("SYNTHETIC retryable reply", { exact: true })).toBeVisible();
  await planner.getByRole("button", { name: "Route optimized", exact: true }).click();
  await planner.getByRole("button", { name: "Diversified hybrid", exact: true }).click();
  await planner.getByRole("button", { name: "Retry experiment creation", exact: true }).click();
  await expect(planner.getByText(/Existing research experiment/)).toBeVisible();
  expect(creates).toHaveLength(2);
  expect(creates[0]).toEqual(creates[1]);
  expect(creates[0]).toMatchObject({
    proposal_id: plans[3].id,
    capital_cad: capital,
    idempotency_key: `funding:${plans[3].id}`,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
});

test("SYNTHETIC: alert policy persists versions and authenticated polling reconnects", async ({
  page,
}) => {
  const writes = await fixtures(page);
  await signIn(page);
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Market coverage and alert policy" }),
  ).toBeVisible();
  await expect(page.getByText("Telegram delivery inactive", { exact: true })).toBeVisible();
  await page.getByLabel("Market allocation profile").selectOption("BALANCED_ADAPTIVE");
  await page.getByLabel("Normal minimum profit · CAD", { exact: true }).fill("12.3456789012345678");
  await page.getByLabel("Enable Telegram notifications").check();
  await page.getByLabel("Minimum Buy Capital · CAD", { exact: true }).fill("25.123456789012345678");
  await page.getByLabel("Ordinary research notifications", { exact: true }).check();
  await page.getByLabel("Quiet-hours ordinary delivery").selectOption("SILENT");
  await page.getByLabel("Report time Edmonton").fill("10:15");
  await page.getByLabel("Weekly owner summary").check();
  await page.getByRole("button", { name: "Save terminal preferences" }).click();
  await expect(page.getByText("Settings version 2 saved.", { exact: false })).toBeVisible();
  expect(
    (writes.find((x) => x.path === "terminal")?.body.settings as typeof preferences).notifications
      .minimum_profit_cad,
  ).toBe("12.3456789012345678");
  const saved = writes.find((x) => x.path === "terminal")?.body.settings as typeof preferences;
  expect(saved.market).toMatchObject({ market_profile: "BALANCED_ADAPTIVE" });
  const notifications = saved.notifications;
  expect(notifications.minimum_buy_capital_cad).toBe("25.123456789012345678");
  expect(notifications.minimum_return_pct).toBe("0.5");
  expect(notifications.research_enabled).toBe(true);
  expect(notifications.emergency_alerts).toBe(false);
  expect(notifications.daily_report_time).toBe("10:15");
  expect(notifications.weekly_report_enabled).toBe(true);
  let requests = 0;
  let disconnected = true;
  const healthy = {
    database: "connected",
    schema_revision: "0009_terminal_settings",
    scanner: { healthy: true, clock: { trusted: true, host_healthy: false } },
    collector: {
      asset_coverage: [
        {
          asset_key: "asset:SYNTH",
          asset: "SYNTH",
          monitored_books: 2,
          connected_venue_groups: 2,
          same_quote_directed_routes: 2,
          transport: { rest: 1, websocket: 1 },
          freshness: { FRESH: 1, AGING: 1 },
          synchronized_ws_books: 1,
          positive_after_cost_routes: 0,
          maximum_executable_buy_cad: "2500.001000000000000001",
          retained_route_evaluation_ms: { p50: 2.5, sample_count: 2 },
        },
      ],
    },
    paper: { healthy: true },
    feeds: [],
    events: [],
    telemetry: {},
  };
  await page.route("**/api/terminal/health", (route) => {
    requests++;
    // Settings also refreshes health after saving. Keep the outage active so a
    // late Settings request cannot consume the target page's sole failure.
    return disconnected
      ? route.fulfill({ status: 503, json: { detail: "SYNTHETIC_TRANSIENT_DISCONNECT" } })
      : route.fulfill({ json: healthy });
  });
  await page.getByRole("link", { name: "System Health", exact: true }).click();
  await expect(page.getByText("SYNTHETIC_TRANSIENT_DISCONNECT", { exact: true })).toBeVisible();
  disconnected = false;
  await page.getByRole("button", { name: "Refresh live data" }).click();
  await expect(page.getByText("0009_terminal_settings", { exact: true })).toBeVisible();
  const coverage = page.getByRole("table", { name: "Market coverage by asset" });
  await expect(coverage.getByRole("rowheader", { name: "SYNTH" })).toBeVisible();
  await expect(coverage.getByTitle("2500.001000000000000001 CAD")).toBeVisible();
  await expect(page.getByText("SYNTHETIC_TRANSIENT_DISCONNECT", { exact: true })).toHaveCount(0);
  expect(requests).toBeGreaterThanOrEqual(2);

  await test.step("actual polling backs off, pauses while hidden and reconnects automatically", async () => {
    // A fresh component separates automatic reconnect from the manual refresh
    // above. Only browser time and SYNTHETIC responses are controlled.
    await page.goto("/overview");
    await page.unroute("**/api/terminal/health");
    await page.clock.install({ time: new Date("2026-01-02T00:00:00Z") });
    await page.clock.pauseAt(new Date("2026-01-02T00:01:00Z"));
    let outage = true;
    const requestTimes: number[] = [];
    await page.route("**/api/terminal/health", async (route) => {
      requestTimes.push(await page.evaluate(() => performance.now()));
      await route.fulfill(
        outage
          ? { status: 503, json: { detail: "SYNTHETIC_AUTOMATIC_RECONNECT" } }
          : { json: healthy },
      );
    });
    await page.goto("/system-health");
    await expect(page.getByText("SYNTHETIC_AUTOMATIC_RECONNECT", { exact: true })).toBeVisible();
    expect(requestTimes).toHaveLength(1);
    await page.clock.runFor(29000);
    expect(requestTimes).toHaveLength(1);
    outage = false;
    await page.clock.runFor(1000);
    await expect(page.getByText("0009_terminal_settings", { exact: true })).toBeVisible();
    await expect(page.getByText("SYNTHETIC_AUTOMATIC_RECONNECT", { exact: true })).toHaveCount(0);
    expect(requestTimes).toHaveLength(2);
    expect(requestTimes[1] - requestTimes[0]).toBe(30000);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.clock.runFor(45000);
    expect(requestTimes).toHaveLength(2);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => requestTimes.length).toBe(3);
    await page.clock.resume();
  });
  await page.setViewportSize({ width: 375, height: 812 });
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

// The compatibility response and the background download each generate a scoped archive.
test("LIVE: analytics and legacy scoped ZIP expose genuine authenticated records", async ({
  page,
}) => {
  test.setTimeout(60000);
  test.skip(process.env.QUANTIX_LIVE_E2E !== "1", "Opt-in live backend verification");
  const { sessionId, cutoff, end } = await openLiveAnalytics(page);
  const query = new URLSearchParams({
    session_id: sessionId,
    start: new Date(cutoff - 60000).toISOString(),
    end,
  });
  const zip = await page.request.get(`/api/reports/export?${query}`);
  expect(zip.status()).toBe(200);
  expect(zip.headers()["content-type"]).toContain("application/zip");
  const bytes = await zip.body();
  expect(bytes.subarray(0, 2).toString()).toBe("PK");
  expect(zip.headers()["content-disposition"]).toContain("quantix-analysis-");
  expect(zip.headers()["x-content-sha256"]).toBe(createHash("sha256").update(bytes).digest("hex"));
});

test("LIVE: selected scoped ZIP downloads as a browser attachment", async ({ page }) => {
  test.setTimeout(60000);
  test.skip(process.env.QUANTIX_LIVE_E2E !== "1", "Opt-in live backend verification");
  const { cutoff } = await openLiveAnalytics(page);
  const minute = Math.floor(cutoff / 60000) * 60000;
  await page
    .getByLabel("Report period start UTC")
    .fill(new Date(minute - 60000).toISOString().slice(0, 16));
  await page.getByLabel("Report period end UTC").fill(new Date(minute).toISOString().slice(0, 16));
  await page.getByRole("button", { name: "Apply period", exact: true }).click();
  await page.getByText("Advanced custom-filter export", { exact: true }).click();
  const exportControl = page.getByRole("button", { name: "Export selected scope", exact: true });
  const pendingDownload = page.waitForEvent("download");
  await exportControl.click();
  const downloaded = await pendingDownload;
  expect(downloaded.suggestedFilename()).toMatch(/^quantix-analysis-.*\.zip$/);
  const savedPath = await downloaded.path();
  expect(savedPath).not.toBeNull();
  expect(readFileSync(savedPath!).subarray(0, 2).toString()).toBe("PK");
});

test("SYNTHETIC: partial valuation and transfer preview keep unavailable CAD distinct", async ({
  page,
}) => {
  const writes = await fixtures(page);
  await page.route(`**/api/paper/sessions/${research}/portfolio`, (route) =>
    route.fulfill({
      json: {
        ...portfolio(true),
        equity_cad: null,
        valuation_status: "PARTIAL",
        valued_subtotal_cad: "9000.123456789012345678",
        unpriced_position_count: 1,
        unpriced_balances: [
          {
            exchange: "synthetic_sell",
            asset: "fiat:EUR",
            quantity: "3.999999999999999999",
            detail_reason: "EUR_CAD_CONVERSION_UNAVAILABLE",
            reason: "CAD_MARK_UNAVAILABLE",
          },
        ],
      },
    }),
  );
  await page.route("**/api/paper/funding/catalog", (route) =>
    route.fulfill({
      json: {
        exchanges: [],
        assets: [
          { id: "synthetic-asset", canonical_key: "asset:SYNTH", symbol: "SYNTH", kind: "crypto" },
        ],
      },
    }),
  );
  let request: Record<string, unknown> | undefined;
  await page.route(`**/api/paper/sessions/${research}/rebalance/preview`, (route) => {
    request = route.request().postDataJSON();
    return route.fulfill({
      json: {
        status: "RESEARCH_TRANSFER_ESTIMATE",
        decision_at: "2026-01-02T11:00:00Z",
        request: {
          from_exchange: "synthetic_buy",
          to_exchange: "synthetic_sell",
          asset: "asset:SYNTH",
          network: "SYNTHETIC_NET",
        },
        no_rebalance_benchmark: {
          source_available_quantity: "5",
          destination_available_quantity: "0",
          destination_deficit: "3.123456789012345678",
          fee_quantity: "0",
        },
        hypothetical_transfer: {
          estimated_available_at: "2026-01-02T11:01:00Z",
          source_debit_quantity: "3.223456789012345678",
          fee_quantity: "0.1",
          delivered_quantity_after_settlement: "3.123456789012345678",
          remaining_destination_deficit: "0",
          transfer_fee_cad: null,
          conversion_cost_cad: "0",
        },
        blockers: [],
        risks: ["OWNER_SYNTHETIC_ASSUMPTIONS"],
      },
    });
  });
  await signIn(page);
  await page.getByRole("link", { name: "Portfolio", exact: true }).click();
  await page.getByLabel("Paper portfolio").selectOption(research);
  const value = page.getByRole("region", { name: "Portfolio valuation coverage" });
  await expect(value.getByText("Partial valuation", { exact: true })).toBeVisible();
  await expect(value.getByTitle("9000.123456789012345678 CAD")).toBeVisible();
  await value.getByText("1 balances without complete marks", { exact: true }).click();
  await expect(value.getByText(/EUR CAD CONVERSION UNAVAILABLE/)).toBeVisible();
  await expect(
    page
      .locator(".paper-summary > div")
      .filter({ hasText: "Current virtual equity" })
      .getByText("Unavailable", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Virtual Funding Planner", exact: true }).click();
  const planner = page.getByRole("region", { name: "Virtual Funding Planner" });
  await planner.getByText("Research rebalance preview", { exact: true }).click();
  await planner.getByLabel("Source exchange", { exact: true }).fill("synthetic_buy");
  await planner.getByLabel("Destination exchange", { exact: true }).fill("synthetic_sell");
  await planner
    .getByRole("combobox", { name: "Canonical asset", exact: true })
    .selectOption("asset:SYNTH");
  await planner
    .getByLabel("Target destination units", { exact: true })
    .fill("3.123456789012345678");
  await planner.getByText("Network and cost assumptions", { exact: true }).click();
  await planner.getByLabel("Network", { exact: true }).fill("SYNTHETIC_NET");
  await planner
    .getByRole("combobox", { name: "Source withdrawal", exact: true })
    .selectOption("available");
  await planner
    .getByRole("combobox", { name: "Destination deposit", exact: true })
    .selectOption("available");
  await planner.getByLabel("Withdrawal fee · asset units", { exact: true }).fill("0.1");
  await planner.getByLabel("Network fee · asset units", { exact: true }).fill("0");
  await planner.getByLabel("Settlement time · seconds", { exact: true }).fill("60");
  await planner.getByRole("button", { name: "Preview rebalance", exact: true }).click();
  const transfer = planner.getByRole("table", { name: "Rebalance versus no transfer" });
  await expect(
    transfer
      .getByRole("row")
      .filter({ hasText: "CAD value of fees" })
      .getByText("Unavailable", { exact: true }),
  ).toBeVisible();
  await expect(
    transfer
      .getByRole("row")
      .filter({ hasText: "Source debit" })
      .getByTitle("3.223456789012345678 SYNTH"),
  ).toBeVisible();
  expect(request).toMatchObject({
    asset: "asset:SYNTH",
    target_destination_quantity: "3.123456789012345678",
    network_fee_quantity: "0",
    settlement_seconds: 60,
  });
  expect(writes.some((item) => item.path.endsWith("/rebalance"))).toBe(false);
});
