import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// SYNTHETIC browser-only telemetry. No fixture is written to the application DB.
const password = readFileSync(resolve(__dirname, "../../.secrets/admin-password"), "utf8").trim();

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill("quantix");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Enter workspace" }).click();
  await expect(page).toHaveURL(/\/overview$/, { timeout: 15000 });
}

test("SYNTHETIC: collector age advances without trusting browser wall-clock jumps", async ({
  page,
}) => {
  await signIn(page);
  await page.clock.install();
  await page.route("**/api/exchanges", (route) =>
    route.fulfill({
      json: [
        {
          id: "synthetic-venue",
          slug: "synthetic",
          name: "SYNTHETIC timing venue",
          enabled: true,
          state: "DEGRADED",
          collector_healthy: true,
          public_data_verified: true,
          market_count: 4,
          monitored_count: 4,
          cad_market_count: 1,
          connection_mode: "REST",
          latency_ms: 210,
          last_success_at: "2026-01-01T00:00:00Z",
          freshness_seconds: 1,
          status_generated_at: "2026-01-01T00:00:01Z",
          last_status_check_at: "2026-01-01T00:00:01Z",
          source: "live_collector",
          clock_trusted: true,
          error_code: null,
          failed_requests: 0,
          successful_requests: 10,
          reconnects: 0,
          capabilities: {},
          freshness_counts: { FRESH: 1, AGING: 2, STALE: 1 },
          timings: { request_ms: { p50: 210, p95: null, sample_count: 10 } },
        },
      ],
    }),
  );
  await page.route("**/api/exchanges/synthetic/markets?*", (route) =>
    route.fulfill({ json: { total: 0, markets: [] } }),
  );
  await page.goto("/exchanges");
  const row = page.getByRole("row").filter({ hasText: "SYNTHETIC timing venue" });
  await expect(row.getByText("1.0s", { exact: true })).toBeVisible();
  await expect(row.getByText("210 ms last REST call", { exact: true })).toBeVisible();
  await page.clock.runFor(2100);
  await expect(row.getByText("3.0s", { exact: true })).toBeVisible();
  await page.clock.setSystemTime(new Date(Date.now() + 80000));
  await page.clock.runFor(1000);
  await expect(row.getByText("4.0s", { exact: true })).toBeVisible();
  await expect(page.getByText(/Status generated:/)).toContainText("2026-01-01 00:00:01 UTC");
  await expect(row.getByText(/Collector received/)).toContainText("00:00:00 UTC");
  await page.getByText("Connector timing and freshness").click();
  await expect(page.getByText("1 / 2 / 1", { exact: true })).toBeVisible();
});

for (const trusted of [false, true]) {
  test(`SYNTHETIC: future age never displays as fresh or zero (clock trusted=${trusted})`, async ({
    page,
  }) => {
    await signIn(page);
    await page.clock.install();
    await page.route("**/api/exchanges", (route) =>
      route.fulfill({
        json: [
          {
            id: "synthetic",
            slug: "synthetic",
            name: "SYNTHETIC untrusted venue",
            enabled: true,
            state: "SUSPICIOUS",
            collector_healthy: false,
            public_data_verified: false,
            market_count: 1,
            monitored_count: 1,
            cad_market_count: 0,
            connection_mode: "REST",
            latency_ms: null,
            last_success_at: null,
            freshness_seconds: -0.1,
            clock_trusted: trusted,
            error_code: "UTC_CLOCK_UNTRUSTED",
            failed_requests: 0,
            successful_requests: 0,
            reconnects: 0,
            capabilities: {},
          },
        ],
      }),
    );
    await page.route("**/api/exchanges/synthetic/markets?*", (route) =>
      route.fulfill({ json: { total: 0, markets: [] } }),
    );
    await page.goto("/exchanges");
    const row = page.getByRole("row").filter({ hasText: "SYNTHETIC untrusted venue" });
    await expect(
      row.getByText(trusted ? "Unavailable" : "Clock untrusted", { exact: true }),
    ).toBeVisible();
    await page.clock.runFor(2100);
    await expect(
      row.getByText(trusted ? "Unavailable" : "Clock untrusted", { exact: true }),
    ).toBeVisible();
    await expect(row.getByText("0.0s", { exact: true })).toHaveCount(0);
    await expect(row.getByText("Retained status snapshot", { exact: true })).toBeVisible();
  });
}

test("SYNTHETIC: native stream synchronization leaves stale receipt age visible", async ({
  page,
}) => {
  const marketId = "aaaaaaaa-0000-4000-8000-000000000008";
  const slug = "synthetic_native";
  const venue = "SYNTHETIC depth venue";
  let trusted = true;
  await signIn(page);
  await page.route("**/api/exchanges", (route) =>
    route.fulfill({
      json: [
        {
          id: "synthetic-native-venue",
          slug,
          name: venue,
          enabled: true,
          state: "DEGRADED",
          collector_healthy: true,
          public_data_verified: true,
          market_count: 2,
          monitored_count: 2,
          cad_market_count: 0,
          connection_mode: "WebSocket + REST",
          latency_ms: 310,
          last_success_at: "2026-01-01T00:00:00Z",
          freshness_seconds: 31,
          status_generated_at: "2026-01-01T00:00:31Z",
          source: "live_collector",
          clock_trusted: trusted,
          error_code: null,
          failed_requests: 0,
          successful_requests: 3,
          reconnects: 1,
          capabilities: {},
          native_stream: {
            subscribed: ["SOL/USD", "ETH/USD"],
            synchronized: 2,
            cached_stream_books: 2,
            valid_cached_stream_books: 1,
            book_age_seconds: { sample_count: 2, p50: 41.9, p95: null, p99: null, maximum: 67.6 },
            counters: { rest_on_stream_market: 3, resynchronizations: 1 },
            market_tiers: { "SOL/USD": "FAST_LANE", "ETH/USD": "STANDARD" },
            last_error: "SYNTHETIC_SEQUENCE_GAP",
          },
        },
      ],
    }),
  );
  await page.route(`**/api/exchanges/${slug}/markets?*`, (route) =>
    route.fulfill({
      json: {
        total: 1,
        markets: [
          {
            id: marketId,
            symbol: "SOL/USD",
            base_symbol: "SOL",
            quote_symbol: "USD",
            monitored: true,
          },
        ],
      },
    }),
  );
  await page.route(`**/api/markets/${marketId}/book`, (route) =>
    route.fulfill({
      json: {
        market_id: marketId,
        exchange: slug,
        symbol: "SOL/USD",
        base_key: "asset:SOL",
        quote_key: "fiat:USD",
        source: `${slug}_ws`,
        normalized: {
          bids: [{ price: "1", quantity: "2" }],
          asks: [{ price: "2", quantity: "3" }],
        },
        received_at: "2025-12-31T23:59:24Z",
        status_generated_at: "2026-01-01T00:00:31Z",
        exchange_timestamp: null,
        latency_ms: null,
        state: "STALE",
        reasons: ["SYNTHETIC_STALE_BOOK"],
        freshness: { classification: "STALE" },
        storage_origin: "live_memory",
        age_ms: 67600,
        verification: {},
      },
    }),
  );
  await page.goto("/exchanges");
  const row = page.getByRole("row").filter({ hasText: venue });
  await expect(row.getByText("WebSocket sync 2 / 2", { exact: true })).toBeVisible();
  await expect(page.getByText(/Collector tier Fast lane/)).toBeVisible();
  await expect(page.getByText(`${venue} WebSocket · live cache`, { exact: true })).toBeVisible();
  await expect(page.getByText("Stream · no REST timing", { exact: true })).toBeVisible();
  await expect(page.getByText("STALE", { exact: true })).toBeVisible();
  await page.getByText("Connector timing and freshness", { exact: true }).click();
  await expect(page.getByText("2 of 2 subscriptions synchronized", { exact: true })).toBeVisible();
  await expect(page.getByText("1 / 2", { exact: true })).toBeVisible();
  await expect(page.getByText("41.9s / 1m 7s (n=2)", { exact: true })).toBeVisible();
  await expect(page.getByText("3 / 1", { exact: true })).toBeVisible();
  await expect(page.getByText("SYNTHETIC SEQUENCE GAP", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
  trusted = false;
  await page.getByRole("button", { name: "Refresh feeds", exact: true }).click();
  const receiptAge = page.locator("dl > div").filter({ hasText: "WebSocket book receipt age" });
  await expect(receiptAge.getByText("Clock untrusted", { exact: true })).toBeVisible();
  await expect(row.getByText("WebSocket sync 2 / 2", { exact: true })).toBeVisible();
});
