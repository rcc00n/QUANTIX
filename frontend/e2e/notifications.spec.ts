import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// SYNTHETIC browser-only notification/evidence fixtures. All changes are intercepted;
// no pairing, provider call, queue item or financial record is written to the app DB.
const id = "aaaaaaaa-0000-4000-8000-000000000006";
const other = "bbbbbbbb-0000-4000-8000-000000000006";
async function signIn(page: Page) {
  await page.goto("/login");
  await page
    .getByLabel("Username", { exact: true })
    .fill(process.env.QUANTIX_E2E_USERNAME || "quantix");
  await page
    .getByLabel("Password", { exact: true })
    .fill(
      process.env.QUANTIX_E2E_PASSWORD ||
        readFileSync(resolve(__dirname, "../../.secrets/admin-password"), "utf8").trim(),
    );
  await page.getByRole("button", { name: "Enter workspace" }).click();
  await expect(page).toHaveURL(/\/overview$/);
}
function connection(paired = false) {
  return {
    paired,
    pairing: paired
      ? { id, username: "SYNTHETIC_owner", approved_at: "2026-01-02T00:00:00Z", paused: false }
      : null,
    pending_challenges: [] as {
      id: string;
      expires_at: string;
      requested: boolean;
      telegram_username: string;
      requested_at: string;
    }[],
    worker: {
      heartbeat_at: "2026-01-02T00:00:00Z",
      healthy: true,
      bot_username: "SYNTHETIC_quantix_bot",
      error_code: null,
      webhook_conflict: false,
    },
    telegram_enabled: true,
    delivery_active: paired,
    providers: {
      telegram: { available: true },
      push: { enabled: false, reason: "PAID_DISABLED" },
      voice: { enabled: false, reason: "PAID_DISABLED" },
    },
    queue_counts: { QUEUED: 1, DELIVERED: 2, EXPIRED: 1 },
  };
}

test("SYNTHETIC: secure dashboard pairing, pause/resume and revoke use real route contracts", async ({
  page,
}) => {
  let status = connection();
  const writes: { path: string; body: unknown }[] = [];
  await page.route("**/api/notifications/**", async (route) => {
    const path = new URL(route.request().url()).pathname.split("/notifications/")[1];
    if (route.request().method() === "POST")
      writes.push({ path, body: route.request().postDataJSON() });
    if (path === "pairing/challenge") {
      status.pending_challenges = [
        {
          id,
          expires_at: "2099-01-02T00:10:00Z",
          requested: true,
          telegram_username: "SYNTHETIC_owner",
          requested_at: "2026-01-02T00:00:00Z",
        },
      ];
      return route.fulfill({
        json: {
          challenge_id: id,
          challenge: "SYNTHETIC_challenge",
          start_url: "https://t.me/SYNTHETIC_quantix_bot?start=SYNTHETIC_challenge",
          expires_at: "2099-01-02T00:10:00Z",
        },
      });
    }
    if (path === "pairing/approve") status = connection(true);
    if (path === "pairing/revoke") status = connection();
    if (path === "pause" && status.pairing) {
      status.pairing.paused = true;
      status.delivery_active = false;
    }
    if (path === "resume" && status.pairing) {
      status.pairing.paused = false;
      status.delivery_active = true;
    }
    if (path.startsWith("history")) return route.fulfill({ json: { items: [], limit: 30 } });
    if (path === "preview")
      return route.fulfill({ json: { message: null, reasons: ["NO_EVALUATION"] } });
    return route.fulfill({ json: status });
  });
  await signIn(page);
  await page.goto("/signals");
  await expect(page.getByText("No Telegram account paired", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Send owner test notification" })).toBeDisabled();
  await page.getByRole("button", { name: "Create pairing link" }).click();
  await expect(page.getByRole("link", { name: "Continue in Telegram" })).toHaveAttribute(
    "href",
    /start=SYNTHETIC_challenge/,
  );
  await page.getByRole("button", { name: "Approve pairing" }).click();
  await expect(page.getByText("@SYNTHETIC_owner", { exact: true })).toBeVisible();
  expect(writes.find((item) => item.path === "pairing/approve")?.body).toEqual({
    challenge_id: id,
  });
  await page.getByRole("button", { name: "Pause notifications" }).click();
  await expect(page.getByRole("button", { name: "Resume notifications" })).toBeVisible();
  await page.getByRole("button", { name: "Resume notifications" }).click();
  await expect(page.getByRole("button", { name: "Pause notifications" })).toBeVisible();
  await page.getByRole("button", { name: "Revoke Telegram pairing" }).click();
  await expect(page.getByText("No Telegram account paired", { exact: true })).toBeVisible();
  expect(writes.map((item) => item.path)).toEqual([
    "pairing/challenge",
    "pairing/approve",
    "pause",
    "resume",
    "pairing/revoke",
  ]);
  await page.setViewportSize({ width: 375, height: 812 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
});

test("SYNTHETIC: delivery preview, history and explicit owner test stay separate from trades", async ({
  page,
}) => {
  const writes: string[] = [];
  await page.route("**/api/notifications/**", async (route) => {
    const path = new URL(route.request().url()).pathname.split("/notifications/")[1];
    if (route.request().method() === "POST") {
      writes.push(path);
      return route.fulfill({
        json: path === "test" ? { id, status: "QUEUED" } : { acknowledged: true },
      });
    }
    if (path === "status") return route.fulfill({ json: connection(true) });
    if (path === "preview")
      return route.fulfill({
        json: {
          message:
            "QUANTIX — RESEARCH ONLY\nSYNTHETIC BTC/CAD\nBuy Capital: C$6.71\nNot actionable: account eligibility unverified",
          eligible: false,
          reasons: ["MINIMUM_BUY_CAPITAL", "ELIGIBILITY_UNVERIFIED"],
          classification: "RESEARCH_ONLY",
          evaluation_id: id,
        },
      });
    if (path.startsWith("history"))
      return route.fulfill({
        json: {
          items: [id, other].map((alertId, i) => ({
            id: alertId,
            alert_id: alertId,
            kind: "URGENT",
            status: "DELIVERED",
            created_at: "2026-01-02T00:00:00Z",
            expires_at: "2099-01-02T00:00:00Z",
            delivered_at: "2026-01-02T00:00:01Z",
            error_code: null,
            attempts: 1,
            message: `SYNTHETIC notification ${i + 1}`,
          })),
          limit: 30,
        },
      });
    return route.fulfill({ status: 404, json: { detail: "SYNTHETIC_UNKNOWN_ROUTE" } });
  });
  await signIn(page);
  await page.goto("/signals");
  await expect(page.getByText("SYNTHETIC BTC/CAD", { exact: false })).toBeVisible();
  await expect(page.getByText("MINIMUM BUY CAPITAL", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Send owner test notification" }).click();
  await expect(page.getByText("Test notification queued.", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Acknowledge alert" }).first().click();
  await page.getByRole("button", { name: "Cancel alert" }).last().click();
  expect(writes).toEqual(["test", `${id}/acknowledge`, `${other}/cancel`]);
  await expect(page.getByText("Disabled · separate authorization required")).toHaveCount(2);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
});