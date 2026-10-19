import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Explicitly SYNTHETIC browser-only responses; never persisted as live exchange data.
const synthetic = {
  id: "aaaaaaaa-0000-4000-8000-000000000003",
  status: "RESEARCH_ONLY",
  actionable: false,
  base_asset: "SYNTHETIC_BTC",
  buy_exchange: "synthetic_buy",
  sell_exchange: "synthetic_sell",
  buy_symbol: "BTC/CAD",
  sell_symbol: "BTC/CAD",
  detected_at: "2025-01-01T00:00:00Z",
  updated_at: "2025-01-01T00:00:01Z",
  expires_at: "2025-01-01T00:00:30Z",
  quantity: "0.000123456789012345",
  buy_price: "1000.123456789012345678",
  sell_price: "1010.123456789012345678",
  net_profit_cad: "0.012345678901234567",
  net_return_pct: "0.5",
  acquisition_cost_cad: "0.2",
  reasons: ["HOST_CLOCK_UNHEALTHY", "FEES_UNVERIFIED"],
  eligibility: "UNVERIFIED",
  calculation_version: "synthetic-test-v1",
  costs: { buy_fee_cad: "0.001", sell_fee_cad: "0.002" },
  evidence: { source: "SYNTHETIC_BROWSER_FIXTURE" },
  budget_results: [],
  lifecycle: [{ state: "RESEARCH_ONLY", reason: "SYNTHETIC_TEST" }],
};

async function signIn(page: import("@playwright/test").Page) {
  const password =
    process.env.QUANTIX_E2E_PASSWORD ||
    readFileSync(resolve(__dirname, "../../.secrets/admin-password"), "utf8").trim();
  await page.goto("/login");
  await page
    .getByLabel("Username", { exact: true })
    .fill(process.env.QUANTIX_E2E_USERNAME || "quantix");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Enter workspace" }).click();
  await expect(page).toHaveURL(/\/overview$/);
}

test("SYNTHETIC: radar preserves exact amounts, confidence gates, filters and recheck", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let lastQuery = new URLSearchParams();
  let rechecked = false;
  await page.route("**/api/opportunities**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown;
    if (url.pathname.endsWith("/budgets"))
      data = { budgets_cad: ["100", "1000"], default_budget_cad: "1000" };
    else if (url.pathname.endsWith("/diagnostics"))
      data = {
        healthy: true,
        clock_healthy: false,
        metrics: {
          monitored_markets: 2,
          compatible_intersections: 1,
          verified_market: 0,
          research_only: 1,
        },
      };
    else if (url.pathname.endsWith("/size-diagnostics"))
      data = {
        evaluated_at: "2025-01-01T00:00:01Z",
        selected_maximum_buy_budget_cad: "1000",
        best: {
          acquisition_cost_cad: "6.71",
          net_profit_cad: "0.5",
          net_return_pct: "7.45",
          sell_base_required: "0.0067",
        },
        comparisons: ["50", "100", "250", "500", "1000"].map((value) => ({
          requested_buy_capital_cad: value,
          supported: false,
          buy_capital_cad: null,
          estimated_net_profit_cad: null,
          estimated_net_return_pct: null,
          required_sell_inventory: null,
          reasons: ["TARGET_EXCEEDS_BUY_OBSERVED_DEPTH"],
        })),
        explanations: ["SELECTED_OPTIMUM_USES_LESS_THAN_MAXIMUM_BUY_BUDGET"],
        optimizer: { optimality: "BOUNDED", evaluated_count: 12 },
        market_constraints: { sell_inventory_currency: "SYNTHETIC_BTC", buy_quote_currency: "CAD" },
      };
    else if (url.pathname.endsWith("/recheck")) {
      rechecked = true;
      data = { status: "QUEUED" };
    } else if (url.pathname.endsWith(synthetic.id)) data = synthetic;
    else {
      lastQuery = url.searchParams;
      data = { items: [synthetic], total: 1, limit: 25, offset: 0 };
    }
    await route.fulfill({ json: data });
  });
  await signIn(page);
  await page.getByRole("link", { name: "Opportunity Radar", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Cross-exchange radar" })).toBeVisible();
  await expect(page.getByText("Clock confidence prevents actionable calculations")).toBeVisible();
  await expect(
    page
      .getByRole("table", { name: "Cross-exchange opportunities" })
      .getByTitle("Exact value: 1000.123456789012345678", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("table", { name: "Cross-exchange opportunities" })
      .getByTitle("Exact value: 1000.123456789012345678", { exact: true }),
  ).toHaveText("≈1,000.12345678");
  await expect(page.getByRole("region", { name: "Opportunity detail" })).toBeVisible();
  await expect(page.getByText("Not actionable", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Compare retained sizes" }).click();
  await expect(page.getByRole("table", { name: "Retained size comparison" })).toBeVisible();
  await expect(
    page.getByRole("table", { name: "Retained size comparison" }).getByTitle("6.71"),
  ).toBeVisible();
  await expect(page.getByText("TARGET EXCEEDS BUY OBSERVED DEPTH", { exact: true })).toHaveCount(5);
  await page
    .getByRole("combobox", { name: "Capital budget · CAD", exact: true })
    .selectOption("100");
  await page.getByLabel("Asset", { exact: true }).fill("btc");
  await page
    .getByRole("combobox", { name: "Confidence", exact: true })
    .selectOption("RESEARCH_ONLY");
  await expect.poll(() => lastQuery.get("budget_cad")).toBe("100");
  await expect.poll(() => lastQuery.get("asset")).toBe("BTC");
  await expect.poll(() => lastQuery.get("status")).toBe("RESEARCH_ONLY");
  await page.locator("summary").filter({ hasText: "Advanced filters" }).click();
  await page.getByLabel("Minimum net CAD profit", { exact: true }).fill("0.01");
  await page.getByRole("button", { name: "Apply filters", exact: true }).click();
  await expect.poll(() => lastQuery.get("min_profit_cad")).toBe("0.01");
  await page.getByRole("button", { name: "Recheck books" }).click();
  await expect.poll(() => rechecked).toBe(true);
  await expect(page.getByText("Fresh evidence requested.", { exact: false })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  expect(errors).toEqual([]);
});

test("LIVE: authenticated radar exposes actual scanner diagnostics without invented results", async ({
  page,
}) => {
  test.skip(process.env.QUANTIX_LIVE_E2E !== "1", "Opt-in live scanner verification");
  await signIn(page);
  await page.getByRole("link", { name: "Opportunity Radar", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Cross-exchange radar" })).toBeVisible();
  const health = await page.request.get("/api/opportunities/diagnostics");
  expect(health.status()).toBe(200);
  const diagnostics = await health.json();
  expect(typeof diagnostics.healthy).toBe("boolean");
  let response = await page.request.get("/api/opportunities?limit=5&budget_cad=1000");
  // The page's own sizing request shares a bounded gate with this independent
  // inspection. Respect its advertised backoff instead of bypassing resource limits.
  for (let attempt = 0; response.status() === 429 && attempt < 4; attempt++) {
    const delay = Math.min(3, Math.max(1, Number(response.headers()["retry-after"] || "2")));
    await response.dispose();
    await page.waitForTimeout(delay * 1000);
    response = await page.request.get("/api/opportunities?limit=5&budget_cad=1000");
  }
  expect(response.status()).toBe(200);
  const result = await response.json();
  expect(Array.isArray(result.items)).toBe(true);
  for (const row of result.items) {
    expect(row.actionable).toBe(false);
    expect(row.buy_exchange).not.toBe(row.sell_exchange);
    expect(row.net_profit_cad === null || typeof row.net_profit_cad === "string").toBe(true);
  }
  await expect(page.getByRole("table", { name: "Cross-exchange opportunities" })).toBeVisible();
});