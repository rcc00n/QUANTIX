import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Explicitly SYNTHETIC browser-only records. No fixture funds/fills enter PostgreSQL.
const validatedId = "aaaaaaaa-0000-4000-8000-000000000004";
const researchId = "bbbbbbbb-0000-4000-8000-000000000004";
const tradeId = "cccccccc-0000-4000-8000-000000000004";
const strategy = {
  schema_version: "2.0",
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

test("SYNTHETIC: paper balances, research isolation, evidence and persistent strategy controls", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let researchSettings = { ...strategy, preset: "Aggressive Research", mode: "RESEARCH_SHADOW" };
  let status = "ACTIVE";
  let saved: Record<string, unknown> | null = null;
  const sessions = () => ({
    items: [
      {
        id: validatedId,
        name: "SYNTHETIC validated portfolio",
        classification: "VALIDATED",
        status: "ACTIVE",
        mode: "PRE_FUNDED",
        initial_equity_cad: "10000",
        started_at: "2025-01-01T00:00:00Z",
        strategy_version_id: "synthetic-version-1",
        settings: strategy,
      },
      {
        id: researchId,
        name: "SYNTHETIC research portfolio",
        classification: "RESEARCH",
        status,
        mode: "RESEARCH_SHADOW",
        initial_equity_cad: "10000",
        started_at: "2025-01-01T00:00:00Z",
        strategy_version_id: saved ? "synthetic-version-2" : "synthetic-version-1",
        settings: researchSettings,
      },
    ],
    total: 2,
  });
  const trade = {
    id: tradeId,
    status: "PARTIAL",
    classification: "RESEARCH",
    mode: "RESEARCH_SHADOW",
    requested_at: "2025-01-01T00:00:00Z",
    eligible_at: "2025-01-01T00:00:30Z",
    completed_at: "2025-01-01T00:00:31Z",
    reason: "SYNTHETIC_SELL_LEG_INSUFFICIENT_DEPTH",
    state: { remaining_exposure: "0.1 SYNTH" },
    requested_quantity: "0.1",
  };
  await page.route("**/api/strategies", (route) =>
    route.fulfill({
      json: {
        current: { id: "synthetic-v1", version: 1, settings: strategy },
        presets: {
          Balanced: strategy,
          Conservative: { ...strategy, preset: "Conservative" },
          "Aggressive Research": researchSettings,
        },
        reaction_delays: [5, 15, 30, 60, 120],
      },
    }),
  );
  await page.route("**/api/paper/**", async (route) => {
    const url = new URL(route.request().url());
    const research =
      url.pathname.includes(researchId) || url.searchParams.get("session_id") === researchId;
    if (url.pathname.endsWith("/strategy")) {
      expect(route.request().method()).toBe("PUT");
      saved = route.request().postDataJSON();
      researchSettings = saved as typeof researchSettings;
      await route.fulfill({ json: { id: "synthetic-version-2", settings: saved } });
    } else if (url.pathname.endsWith("/pause")) {
      status = "PAUSED";
      await route.fulfill({ json: { status } });
    } else if (url.pathname.endsWith("/resume")) {
      status = "ACTIVE";
      await route.fulfill({ json: { status } });
    } else if (url.pathname.endsWith("/sessions")) await route.fulfill({ json: sessions() });
    else if (url.pathname.endsWith("/portfolio"))
      await route.fulfill({
        json: {
          equity_cad: research ? "9999.250000000000000000" : "10000",
          net_simulated_pnl_cad: research ? "-0.75" : "0",
          realized_trading_pnl_cad: research ? "-0.5" : "0",
          realized_fx_pnl_cad: "0",
          unrealized_pnl_cad: research ? "-0.25" : "0",
          fees_cad: research ? "0.1" : "0",
          expenses_cad: research ? "0.15" : "0",
          no_trading_benchmark_cad: "10000",
          strategy_excess_cad: research ? "-0.75" : "0",
          drawdown_pct: research ? "0.0075" : "0",
          capital_utilization_pct: "0",
          portfolio_return_pct: research ? "-0.0075" : "0",
          balances: [
            {
              exchange: "SYNTHETIC",
              asset: "CAD",
              quantity: research ? "5000" : "10000",
              available_quantity: research ? "5000" : "10000",
              reserved_quantity: "0",
              cost_basis_cad: research ? "5000" : "10000",
              market_value_cad: research ? "5000" : "10000",
            },
            ...(research
              ? [
                  {
                    exchange: "SYNTHETIC",
                    asset: "SYNTH",
                    quantity: "49.9925",
                    available_quantity: "49.9925",
                    reserved_quantity: "0",
                    cost_basis_cad: "4999.5",
                    market_value_cad: "4999.25",
                  },
                ]
              : []),
          ],
          limitations: research ? ["SYNTHETIC_RESEARCH_ASSUMPTIONS_UNVERIFIED"] : [],
        },
      });
    else if (url.pathname.endsWith("/metrics"))
      await route.fulfill({
        json: {
          counts: {
            PARTIAL: research ? 1 : 0,
            MISSED: research ? 2 : 0,
            FAILED: research ? 1 : 0,
          },
          scheduled_simulations: 0,
          completed_simulations: 0,
          partially_completed: research ? 1 : 0,
          missed_simulations: research ? 2 : 0,
          failed_simulations: research ? 1 : 0,
          expired_simulations: 0,
          maximum_drawdown_pct: "0.0075",
          win_rate_pct: null,
        },
      });
    else if (url.pathname.endsWith(tradeId))
      await route.fulfill({
        json: {
          trade,
          orders: [
            { id: "synthetic-buy", side: "buy", quantity: "0.1", status: "partial", reason: null },
            { id: "synthetic-sell", side: "sell", quantity: "0.1", status: "filled", reason: null },
          ],
          fills: [
            {
              id: "synthetic-buy-fill",
              order_id: "synthetic-buy",
              quantity: "0.09",
              price: "100",
              fee_quantity: "0.01",
              fee_version_id: "synthetic-buy-fee",
              fee_cad: null,
              executed_at: "2025-01-01T00:00:31Z",
            },
            {
              id: "synthetic-sell-fill",
              order_id: "synthetic-sell",
              quantity: "0.1",
              price: "90",
              fee_quantity: "0.01",
              fee_version_id: "synthetic-sell-fee",
              fee_cad: null,
              executed_at: "2025-01-01T00:00:31Z",
            },
          ],
          fee_versions: [
            { id: "synthetic-buy-fee", assumptions: { currency: "quote" } },
            { id: "synthetic-sell-fee", assumptions: { currency: "quote" } },
          ],
          observations: [
            {
              order_id: "synthetic-buy",
              received_at: "2025-01-01T00:00:31Z",
              source_at: "2025-01-01T00:00:30Z",
              payload: {
                book: {
                  exchange: "synthetic_buy",
                  symbol: "SYNTH/USDT",
                  base_key: "asset:SYNTH",
                  quote_key: "asset:USDT",
                  state: "SUSPICIOUS",
                  source: "SYNTHETIC_BROWSER_FIXTURE",
                  normalized: {
                    bids: [{ price: "99", quantity: "10" }],
                    asks: [{ price: "100", quantity: "10" }],
                  },
                },
              },
            },
            {
              order_id: "synthetic-sell",
              received_at: "2025-01-01T00:00:31Z",
              source_at: "2025-01-01T00:00:30Z",
              payload: {
                book: {
                  exchange: "synthetic_sell",
                  symbol: "SYNTH/EUR",
                  base_key: "asset:SYNTH",
                  quote_key: "fiat:EUR",
                  state: "SUSPICIOUS",
                  source: "SYNTHETIC_BROWSER_FIXTURE",
                  normalized: {
                    bids: [{ price: "90", quantity: "10" }],
                    asks: [{ price: "91", quantity: "10" }],
                  },
                },
              },
            },
          ],
          events: [{ status: "PARTIAL", evidence: "SYNTHETIC_BROWSER_FIXTURE" }],
        },
      });
    else
      await route.fulfill({
        json: { items: research ? [trade] : [], total: research ? 1 : 0, limit: 25, offset: 0 },
      });
  });
  await signIn(page);
  await page.getByRole("link", { name: "Paper Trading", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Paper trading activity" })).toBeVisible();
  await expect(page.getByLabel("Paper portfolio")).toHaveValue(validatedId);
  await expect(page.getByText("No recorded simulations", { exact: true })).toBeVisible();
  await expect(page.getByText(/Hypothetical research portfolio/)).toHaveCount(0);
  await page.getByLabel("Paper portfolio").selectOption(researchId);
  await expect(page.getByText(/Hypothetical research portfolio/)).toBeVisible();
  await expect(
    page.getByLabel("Simulation outcomes").getByText("2", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTitle("9999.250000000000000000 CAD", { exact: true })).toHaveText(
    "9,999.25 CAD",
  );
  await page.getByRole("button", { name: "cccccccc", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Execution evidence · cccccccc" })).toBeVisible();
  await expect(page.getByText("SYNTHETIC_BROWSER_FIXTURE", { exact: false }).first()).toBeVisible();
  const buyLeg = page.locator(".execution-leg").filter({ hasText: "Simulated BUY leg" });
  const sellLeg = page.locator(".execution-leg").filter({ hasText: "Simulated SELL leg" });
  await expect(buyLeg.getByText("USDT per SYNTH", { exact: true })).toBeVisible();
  await expect(sellLeg.getByText("EUR per SYNTH", { exact: true })).toBeVisible();
  await expect(buyLeg.getByTitle("100 USDT", { exact: true })).toBeVisible();
  await expect(sellLeg.getByTitle("90 EUR", { exact: true })).toBeVisible();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download JSON evidence", exact: true }).click();
  expect((await downloaded).suggestedFilename()).toBe(`quantix-paper-${tradeId}.json`);
  await page.getByRole("button", { name: "Pause simulations" }).click();
  await expect(page.getByRole("button", { name: "Resume simulations" })).toBeVisible();
  await page.getByRole("link", { name: "Inspect virtual balances", exact: true }).click();
  await expect(page.getByLabel("Paper portfolio")).toHaveValue(researchId);
  await expect(page.getByRole("table", { name: "Virtual balances" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "49.9925", exact: true }).first()).toBeVisible();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Strategy management", exact: true }),
  ).toBeVisible();
  await page.getByLabel("Paper portfolio").selectOption(researchId);
  await expect(page.getByLabel("Simulation mode")).toHaveValue("RESEARCH_SHADOW");
  await page.getByLabel("Trade amount CAD", { exact: true }).fill("123.456789012345678");
  await page.getByLabel("Reaction delay seconds").fill("15");
  await page.getByRole("button", { name: "Save strategy version" }).click();
  await expect.poll(() => saved?.trade_amount_cad).toBe("123.456789012345678");
  expect((saved as Record<string, unknown> | null)?.reaction_delay_seconds).toBe(15);
  expect((saved as Record<string, unknown> | null)?.mode).toBe("RESEARCH_SHADOW");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  expect(errors).toEqual([]);
});