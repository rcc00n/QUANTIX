import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Uses the local operator only for authentication. No financial records are seeded.
const password =
  process.env.QUANTIX_E2E_PASSWORD ||
  readFileSync(resolve(__dirname, "../../.secrets/admin-password"), "utf8").trim();
const username = process.env.QUANTIX_E2E_USERNAME || "quantix";

async function signIn(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Enter workspace" }).click();
  await expect(page).toHaveURL(/\/overview$/);
}

test("dashboard and private API require a valid session", async ({ page, request }) => {
  await page.goto("/overview");
  await expect(page).toHaveURL(/\/login$/);
  const response = await request.get("/api/system/status");
  expect(response.status()).toBe(401);
  for (const path of [
    "/api/terminal/overview",
    "/api/terminal/health",
    "/api/settings/terminal",
    "/api/notifications/status",
    "/api/notifications/history",
    "/api/notifications/preview",
    "/api/ai/status",
    "/api/analytics/sessions/aaaaaaaa-0000-4000-8000-000000000005",
    "/api/reports/export?session_id=aaaaaaaa-0000-4000-8000-000000000005",
  ]) {
    expect((await request.get(path)).status()).toBe(401);
  }
  await page
    .context()
    .addCookies([
      { name: "arbiq_session", value: "synthetic-invalid-token", domain: "127.0.0.1", path: "/" },
    ]);
  await page.goto("/portfolio");
  await expect(page).toHaveURL(/\/login$/);
});

test("login, backend status, navigation, contract download and logout", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await signIn(page);
  await expect(page.getByText("Backend connected", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Command Center.", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Equity versus benchmark" })).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "Analytics", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Portfolio and no-trading benchmark" }),
  ).toBeVisible();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download report contract" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("quantix-report-contract.json");
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Strategy management" })).toBeVisible();
  // Local collector is deliberately paused after cutover. Wait for the actual
  // bounded health read before checking its data, rather than a 5s navigation race.
  const healthResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/terminal/health",
    { timeout: 12000 },
  );
  await page.getByRole("link", { name: "System Health", exact: true }).click();
  expect((await healthResponse).status()).toBe(200);
  await expect(page.getByText(/^00\d{2}_[a-z0-9_]+$/, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/overview");
  await expect(page).toHaveURL(/\/login$/);
  expect(errors).toEqual([]);
});

test("mobile navigation fits the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("link", { name: "Paper Trading", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Paper trading activity" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
});

test("BFF rejects cross-origin writes and unlisted routes", async ({ request }) => {
  expect(
    (
      await request.post("/api/auth/login", {
        headers: { Origin: "https://evil.example" },
        data: {},
      })
    ).status(),
  ).toBe(403);
  expect((await request.get("/api/not-allowlisted")).status()).toBe(404);
  expect((await request.get("/api/auth/login")).status()).toBe(405);
});

test("login rejects an external return destination", async ({ page }) => {
  await page.goto("/login?next=https%3A%2F%2Fevil.example%2F");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Enter workspace" }).click();
  await expect(page).toHaveURL(/\/overview$/);
});

test("LIVE: QUANTIX assets and real normalized books in the explorer", async ({ page }) => {
  test.skip(process.env.QUANTIX_LIVE_E2E !== "1", "Opt-in live public-data verification");
  await signIn(page);
  await expect(page).toHaveTitle("QUANTIX");
  await expect(page.getByRole("img", { name: "QUANTIX", exact: true })).toBeVisible();
  expect((await page.request.get("/brand/quantix-icon-32.png")).status()).toBe(200);
  await page.getByRole("link", { name: "Exchanges", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Public data connections" })).toBeVisible();
  await expect(page.getByLabel("Market", { exact: true })).not.toHaveValue("");
  const marketId = await page.getByLabel("Market", { exact: true }).inputValue();
  const response = await page.request.get(`/api/markets/${marketId}/book`);
  expect(response.status()).toBe(200);
  const book = await response.json();
  expect(book.normalized.bids.length).toBeGreaterThan(0);
  expect(book.normalized.asks.length).toBeGreaterThan(0);
  expect(typeof book.normalized.bids[0].price).toBe("string");
  expect(["rest", `${book.exchange}_ws`]).toContain(book.source);
  expect(book.actionable).toBe(false);
  await expect(page.getByRole("table", { name: "bids order book", exact: true })).toBeVisible();
  await expect(
    page.getByText("Public API access does not establish Canadian trading eligibility.", {
      exact: false,
    }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
});
