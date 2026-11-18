import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// SYNTHETIC status only. The fixture never starts a worker, sends a message or writes a row.
test("SYNTHETIC: System Health separates sustained degradation, recovery and disabled delivery", async ({
  page,
}) => {
  const at = "2026-01-04T00:00:00Z";
  await page.route("**/api/terminal/health", (route) =>
    route.fulfill({
      json: {
        as_of: at,
        scanner: { clock: { trusted: true, host_healthy: true } },
        collector: {
          reliability: {
            schema_version: "quantix-collector-reliability-v1",
            observed_at: at,
            checks: [
              {
                key: "market_coverage",
                state: "HEALTHY",
                reason: null,
                pending_state: null,
                pending_seconds: null,
                metrics: {
                  monitored_books: 20,
                  fresh_books: 10,
                  aging_books: 8,
                  stale_books: 2,
                  unavailable_books: 0,
                  clock_untrusted_books: 0,
                },
              },
              {
                key: "scanner_progress",
                state: "DEGRADED",
                reason: "SYNTHETIC_SCANNER_BACKLOG",
                pending_state: "HEALTHY",
                pending_seconds: 18,
                metrics: {
                  pending_comparisons: 1900,
                  queue_capacity: 2000,
                  oldest_seconds: 3,
                  heartbeat_age_seconds: 1,
                  deferred_markets: 4,
                },
              },
              {
                key: "native_streams",
                state: "DEGRADED",
                reason: "SYNTHETIC_RESYNC",
                pending_state: null,
                pending_seconds: null,
                metrics: {
                  subscribed_markets: 4,
                  synchronized_markets: 3,
                  unsynchronized_exchanges: ["synthetic_venue"],
                },
              },
            ],
          },
        },
        paper: {
          enabled: true,
          healthy: false,
          metrics: { failed_ticks: 2, clock_gated_ticks: 9, failed: 3, partial: 1, missed: 5 },
        },
        paper_valuation: {
          portfolios_sampled: 3,
          valuation_counts: { COMPLETE_FRESH: 1, PARTIAL: 1, STALE: 0, UNKNOWN: 1 },
          latest_checkpoint_at: at,
        },
        events: [
          {
            id: "synthetic-collector-recovery",
            created_at: at,
            event_type: "collector.reliability_recovered",
            severity: "info",
            message: "SYNTHETIC collector freshness recovered",
          },
        ],
      },
    }),
  );
  let workerAvailable = true;
  await page.route("**/api/notifications/status", (route) =>
    route.fulfill({
      json: {
        telegram_enabled: false,
        delivery_active: false,
        paired: true,
        pairing: { username: "synthetic_private_username" },
        pending_challenges: [{ telegram_username: "synthetic_private_challenge" }],
        worker: workerAvailable
          ? {
              healthy: false,
              heartbeat_at: at,
              error_code: "SYNTHETIC_PROVIDER_ERROR",
              webhook_conflict: false,
              bot_username: "synthetic_bot_username",
            }
          : null,
      },
    }),
  );
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
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "System Health", exact: true })
    .click();
  const checks = page.getByRole("table", { name: "Sustained collector checks" });
  const scanner = checks.getByRole("row").filter({ hasText: "Scanner progress" });
  await expect(scanner.getByText("DEGRADED", { exact: false })).toBeVisible();
  await expect(scanner.getByText("Confirming recovery · 18 s", { exact: true })).toBeVisible();
  await expect(scanner.getByText(/1900 \/ 2000 pending/)).toBeVisible();
  await expect(
    checks.getByText(/3 \/ 4 synchronized · Resync needed: synthetic_venue/),
  ).toBeVisible();
  await expect(
    page.getByText("SYNTHETIC collector freshness recovered", { exact: true }),
  ).toBeVisible();
  const telegram = page.getByRole("region", { name: "Telegram worker health" });
  await expect(telegram.getByText("Degraded", { exact: true })).toBeVisible();
  await expect(telegram.getByText("Disabled by policy", { exact: true })).toBeVisible();
  await expect(telegram.getByText("synthetic provider error", { exact: true })).toBeVisible();
  await expect(page.getByText(/synthetic_private_|synthetic_bot_username/)).toHaveCount(0);
  const paper = page.getByRole("region", { name: "Paper execution and valuation" });
  await expect(paper.getByText("3 / 1 / 5", { exact: true })).toBeVisible();
  await expect(paper.getByText("1 / 1 / 0 / 1", { exact: true })).toBeVisible();
  workerAvailable = false;
  await page.reload();
  await expect(telegram.getByText("Disabled by policy", { exact: true })).toBeVisible();
  await expect(telegram.getByText("Unavailable", { exact: true })).toHaveCount(2);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
});
