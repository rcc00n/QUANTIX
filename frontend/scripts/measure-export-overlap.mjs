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

function configuration() {
  const { values } = parseArgs({
    options: {
      url: { type: "string", default: "http://127.0.0.1:3181" },
      phase: { type: "string", default: "idle" },
      "duration-seconds": { type: "string", default: "120" },
      "interval-seconds": { type: "string", default: "5" },
      "wait-seconds": { type: "string", default: "120" },
      "session-id": { type: "string" },
      "radar-budget": { type: "string", default: "1000" },
      browser: { type: "boolean" },
    },
  });
  const url = new URL(values.url);
  if (
    !["127.0.0.1", "[::1]"].includes(url.hostname) ||
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  )
    throw new Error("Use a canonical loopback URL for an isolated local deployment");
  if (!["idle", "export"].includes(values.phase)) throw new Error("Use phase idle or export");
  if (values["session-id"] && !uuid.test(values["session-id"]))
    throw new Error("The requested portfolio ID must be a UUID");
  if (!/^\d{1,8}(?:\.\d{1,18})?$/.test(values["radar-budget"]))
    throw new Error("The Radar budget must be a positive decimal amount");
  return {
    origin: url.origin,
    phase: values.phase,
    duration: boundedSeconds(values["duration-seconds"], "duration", 5, 3600),
    interval: boundedSeconds(values["interval-seconds"], "interval", 3, 60),
    wait: boundedSeconds(values["wait-seconds"], "wait", 0, 1800),
    session: values["session-id"],
    budget: values["radar-budget"],
    browser: values.browser,
  };
}

async function json(api, path) {
  const response = await api.get(path, { maxRedirects: 0 });
  try {
    if (!response.ok()) throw new Error(`HTTP ${response.status()}`);
    return await response.json();
  } finally {
    await response.dispose();
  }
}

async function currentExport(api) {
  try {
    return { known: true, job: (await json(api, "/api/reports/exports/current")).job };
  } catch {
    return { known: false, job: null };
  }
}

async function awaitPhase(api, config) {
  const active = (state) =>
    state.known && state.job?.scope === "ALL_RETAINED" && state.job?.status === "running";
  const idle = (state) => state.known && !activeStatuses.has(state.job?.status);
  const start = performance.now();
  let state = await currentExport(api);
  while (!(config.phase === "export" ? active(state) : idle(state))) {
    if (performance.now() - start >= config.wait) return null;
    await delay(2000);
    state = await currentExport(api);
  }
  return state;
}

function samePhase(initial, observed, phase) {
  if (!observed.known || observed.job?.id !== initial.job?.id) return false;
  return phase === "export"
    ? observed.job?.status === "running"
    : !activeStatuses.has(observed.job?.status);
}

try {
  process.exitCode = await main(configuration());
} catch {
  // Playwright exceptions can contain sensitive request context. Output a stable code only.
  record({ event: "probe_stopped", reason: "invalid_input_or_setup_failure" });
  process.exitCode = 1;
}