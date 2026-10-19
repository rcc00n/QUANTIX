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