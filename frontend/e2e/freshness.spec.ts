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