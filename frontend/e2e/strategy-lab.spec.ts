import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// SYNTHETIC browser fixture. Comparison responses are mocked and create no portfolios or fills.
const ids = [
  "90000000-0000-4000-8000-000000000001",
  "90000000-0000-4000-8000-000000000002",
  "90000000-0000-4000-8000-000000000003",
  "90000000-0000-4000-8000-000000000004",
];
const strategyVersion = "91000000-0000-4000-8000-000000000001";
const fundingTime = "2026-01-01T00:00:00Z";
const start = "2026-01-03T00:00:00.000Z";
const end = "2026-01-04T00:00:00.000Z";
const closingTime = "2026-01-03T23:59:59.000Z";
const synthetic = [
  {
    name: "SYNTHETIC liquid account",
    capital: "10000.000000000000000000",
    profile: "LIQUID_ARBITRAGE",
    classification: "RESEARCH",
  },
  {
    name: "SYNTHETIC hold account",
    capital: "10000",
    profile: "NO_TRADING",
    classification: "RESEARCH",
  },
  {
    name: "SYNTHETIC different capital",
    capital: "50000",
    profile: "HYBRID_OPTIMIZER",
    classification: "RESEARCH",
  },
  {
    name: "SYNTHETIC validated account",
    capital: "10000",
    profile: "BASELINE",
    classification: "VALIDATED",
  },
];

test("SYNTHETIC: Strategy Lab compares independent equal-capital portfolios over one UTC window", async ({
  page,
}) => {
  const requested: unknown[] = [];
  await page.route("**/api/paper/sessions", (route) =>
    route.fulfill({
      json: {
        items: synthetic.map((row, index) => ({
          id: ids[index],
          name: row.name,
          classification: row.classification,
          status: "ACTIVE",
          mode: "RESEARCH_SHADOW",
          initial_equity_cad: row.capital,
          started_at: fundingTime,
          strategy_version_id: strategyVersion,
          settings: { strategy_lab_profile: row.profile },
        })),
      },
    }),
  );
  await page.route("**/api/analytics/sessions/**", (route) =>
    route.fulfill({
      json: { metrics: {}, portfolio: null, items: [], strategy_versions: [] },
    }),
  );
  await page.route("**/api/analytics/strategy-lab/compare", async (route) => {
    expect(route.request().method()).toBe("POST");
    requested.push(route.request().postDataJSON());
    await route.fulfill({
      json: {
        schema_version: "quantix-strategy-lab-v1",
        kind: "FORWARD_PAPER_OBSERVED",
        paper_only: true,
        period: { start, end },
        initial_capital_cad: "10000",
        rows: synthetic.slice(0, 2).map((row, index) => ({
          session_id: ids[index],
          name: row.name,
          profile: row.profile,
          strategy_version_id: strategyVersion,
          strategy_version: 1,
          started_at: fundingTime,
          initial_capital_cad: "10000",
          opening: {
            as_of: start,
            recorded_at: start,
            equity_cad: "10000",
            benchmark_cad: "10000",
            valuation_status: "COMPLETE_FRESH",
            equity_reconciled: true,
          },
          closing: {
            as_of: closingTime,
            recorded_at: closingTime,
            equity_cad: index === 0 ? "9999.987654321098765432" : null,
            benchmark_cad: "10000",
            valuation_status: index === 0 ? "COMPLETE_FRESH" : "PARTIAL",
            equity_reconciled: index === 0 ? true : null,
          },
          valuation_status: index === 0 ? "COMPLETE_FRESH" : "PARTIAL",
          completed_simulations: 0,
          partially_completed: index === 0 ? 1 : 0,
          failed_simulations: 0,
          missed_simulations: 0,
          expired_simulations: 0,
          realized_trading_pnl_cad: index === 0 ? "-0.012345678901234568" : "0",
          realized_fx_pnl_cad: "0",
          fees_cad: "0",
          expenses_cad: "0",
          unrealized_pnl_cad: index === 0 ? "0" : null,
          portfolio_return_pct: index === 0 ? "-0.000123456789012346" : null,
          benchmark_return_pct: "0",
          excess_return_pct: index === 0 ? "-0.000123456789012346" : null,
          strategy_excess_cad: index === 0 ? "-0.012345678901234568" : null,
          drawdown_pct: index === 0 ? "0.000123456789012346" : null,
          capital_utilization_pct: index === 0 ? "4" : "0",
          inventory_exposure_cad: index === 0 ? "500.123456789012345678" : null,
          largest_exchange_share_pct: index === 0 ? "70" : null,
          retained_checkpoint_count: 2,
          complete_checkpoint_count: index === 0 ? 2 : 1,
          ledger_reconciled: true,
          limitations: index === 0 ? [] : ["SYNTHETIC missing FX checkpoint"],
        })),
        limitations: ["SYNTHETIC fixed common observation window"],
      },
    });
  });
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
    .getByRole("link", { name: "Analytics", exact: true })
    .click();
  const lab = page.getByRole("region", { name: "Strategy Lab", exact: true });
  await expect(lab.getByRole("checkbox")).toHaveCount(2);
  await expect(
    lab.getByRole("checkbox", { name: /different capital|validated account/ }),
  ).toHaveCount(0);
  const compare = lab.getByRole("button", { name: "Compare strategy portfolios" });
  await lab.getByRole("checkbox", { name: /SYNTHETIC liquid account/ }).check();
  await expect(compare).toBeDisabled();
  await lab.getByRole("checkbox", { name: /SYNTHETIC hold account/ }).check();
  await lab.getByLabel("Comparison start · UTC").fill("2026-01-03T00:00:00");
  await lab.getByLabel("Comparison end · UTC").fill("2026-01-04T00:00:00");
  await compare.click();
  const comparison = lab.getByRole("region", { name: "Recorded forward paper comparison" });
  const table = comparison.getByRole("table", {
    name: "Independent strategy portfolio comparison",
  });
  await expect(table.getByRole("rowheader")).toHaveCount(2);
  await expect(table.getByTitle("-0.012345678901234568", { exact: true })).toBeVisible();
  const hold = table.getByRole("row").filter({ hasText: "SYNTHETIC hold account" });
  await expect(hold.getByText("Unavailable", { exact: true })).toHaveCount(4);
  await expect(comparison.getByText(/per portfolio/)).toBeVisible();
  await comparison.getByText("SYNTHETIC hold account · valuation and evidence").click();
  await expect(comparison.getByText("SYNTHETIC missing FX checkpoint")).toBeVisible();
  await comparison.getByText("SYNTHETIC liquid account · valuation and evidence").click();
  await expect(comparison.getByTitle("500.123456789012345678 CAD", { exact: true })).toBeVisible();
  expect(requested).toEqual([{ session_ids: ids.slice(0, 2), start, end }]);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
});
