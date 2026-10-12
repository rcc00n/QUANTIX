import { defineConfig } from "@playwright/test";
// AI error-context snapshots may include password input values from a failed login.
// Keep screenshots (password-masked) and ordinary errors without those DOM dumps.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1";
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  reporter: "list",
  use: {
    baseURL: process.env.QUANTIX_E2E_URL || "http://127.0.0.1:3100",
    headless: true,
    screenshot: "only-on-failure",
    trace: "off",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
  },
});
