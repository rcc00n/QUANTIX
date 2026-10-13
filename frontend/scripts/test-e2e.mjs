// Set before Playwright starts or snapshots its worker environment. Failed login
// DOM dumps can include password values; masked screenshots remain available.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const cli = fileURLToPath(new URL("../node_modules/playwright/cli.js", import.meta.url));
const result = spawnSync(process.execPath, [cli, "test", ...process.argv.slice(2)], {
  stdio: "inherit",
  env: { ...process.env, PLAYWRIGHT_NO_COPY_PROMPT: "1" },
});
process.exit(result.status ?? 1);
