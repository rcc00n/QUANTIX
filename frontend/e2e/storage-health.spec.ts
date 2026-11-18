import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

test("SYNTHETIC: storage health distinguishes a measured scratch warning from unknown DB runway", async ({
  page,
}) => {
  const at = "2026-01-04T00:00:00Z";
  await page.route("**/api/terminal/health", (route) =>
    route.fulfill({
      json: {
        as_of: at,
        storage: {
          schema_version: "quantix-storage-health-v1",
          database_state: "OBSERVED",
          sample_age_seconds: 2,
          database_volume_runway_days: null,
          backup_state: "UNAVAILABLE",
          warnings: ["EXPORT_SCRATCH_BELOW_DISK_RESERVE"],
          database: {
            observed_at: at,
            database_bytes: 123456789,
            wal_bytes: 987654321,
            indexes: 6,
            unhealthy_indexes: 0,
            retained_relations: [{ name: "ledger_entries", bytes: 2048, index_bytes: 1024 }],
          },
          export_scratch: { state: "LOW_SPACE", free_bytes: 21, reserve_bytes: 6442450944 },
        },
        telemetry: {
          api: {
            cgroup: {
              scope: "current_process_cgroup_v2",
              memory_charged_bytes: 314572800,
              memory_limit_bytes: 402653184,
              memory_pct_of_limit: 78.125,
              memory_limit_events_total: 3,
              cpu_pct_of_limit: 75,
              cpu_throttled_period_pct: 25,
              oom_kills_total: 0,
            },
          },
        },
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
  const storage = page.getByRole("region", { name: "Database storage and backup health" });
  await expect(storage.getByRole("status")).toHaveText(
    "Export scratch space is below its safety reserve.",
  );
  await expect(storage.getByTitle("123456789 bytes", { exact: true })).toBeVisible();
  await expect(storage.getByTitle("987654321 bytes", { exact: true })).toBeVisible();
  await expect(storage.getByText(/database filesystem free space is outside/)).toBeVisible();
  await expect(storage.getByText(/backup and restore proofs are verified outside/)).toBeVisible();
  await expect(storage.getByRole("rowheader", { name: "ledger entries" })).toBeVisible();
  await expect(page.getByText("Cgroup memory charge / cap")).toBeVisible();
  await expect(page.getByTitle("314572800 bytes", { exact: true })).toBeVisible();
  await expect(page.getByText("CPU cap usage / throttled scheduling periods")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
});
