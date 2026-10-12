import { readFile } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import { parseArgs } from "node:util";
import { chromium, request } from "playwright";

const requestDeadline = 12000;
const activeStatuses = new Set(["queued", "running"]);
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const phaseTiming =
  /^(catalog|collector|retained|projection);dur=(\d{1,9}(?:\.\d{1,3})?)(?:;desc="(\d{1,6}) rows")?$/;
const record = (data) => process.stdout.write(`${JSON.stringify(data)}\n`);
const elapsed = (start) => Math.round((performance.now() - start) * 100) / 100;

function boundedSeconds(raw, name, min, max) {
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max)
    throw new Error(`${name} must be an integer from ${min} to ${max}`);
  return value * 1000;
}

try {
  process.exitCode = await main(configuration());
} catch {
  // Playwright exceptions can contain sensitive request context. Output a stable code only.
  record({ event: "probe_stopped", reason: "invalid_input_or_setup_failure" });
  process.exitCode = 1;
}