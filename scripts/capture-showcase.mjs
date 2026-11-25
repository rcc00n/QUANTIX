/** Capture the unchanged QUANTIX frontend using explicit, loopback-only SYNTHETIC API fixtures. */
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdir, readFile, writeFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { createServer } from 'node:net';
const own = path.dirname(fileURLToPath(import.meta.url));
const defaultRoot = path.resolve(own, '..');
const args = process.argv.slice(2);
const option = (key, fallback) => args.includes(key) ? args[args.indexOf(key) + 1] : fallback;
const root = path.resolve(option('--source-root', defaultRoot));
const output = path.resolve(option('--output', path.join(root, 'docs/assets/showcase')));
const frontendPort = Number(option('--port', '3371')), fixturePort = Number(option('--fixture-port', '3372'));
for (const port of [frontendPort, fixturePort]) if (!Number.isInteger(port) || port < 1024 || port > 65535) throw Error('Use an unprivileged loopback port');
if (frontendPort === fixturePort) throw Error('Use distinct frontend and fixture ports');
for (const privatePath of ['.env', '.env.local', '.env.production', '.env.production.local', '.secrets', '.local', 'frontend/.env', 'frontend/.env.local', 'frontend/.env.production', 'frontend/.env.production.local']) {
  if (await stat(path.join(root, privatePath)).then(() => true, () => false)) throw Error('Use a fresh isolated clone without private environment or application state: ' + privatePath);
}
const requireFrontend = createRequire(path.join(root, 'frontend/package.json'));
const { chromium } = requireFrontend('@playwright/test');
const fixtures = createRequire(import.meta.url)('./showcase-fixtures.cjs');
await stat(path.join(root, 'frontend/.next/BUILD_ID')).catch(() => { throw Error('Build the frontend first: cd frontend && npm ci && npm run build'); });
async function assertLoopbackPortFree(port) {
  await new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once('error', () => reject(Error('Loopback port unavailable: ' + port)));
    probe.listen({ host: '127.0.0.1', port, exclusive: true }, () => probe.close(resolve));
  });
}
await assertLoopbackPortFree(frontendPort);
await assertLoopbackPortFree(fixturePort);
await mkdir(output, { recursive: true });
const framePath = path.join(output, '.frames'); await mkdir(framePath, { recursive: true });
const transport = await fixtures.createFixtureServer({ port: fixturePort });
const next = spawn(process.execPath, [requireFrontend.resolve('next/dist/bin/next'), 'start', '--hostname', '127.0.0.1', '--port', String(frontendPort)], {
  cwd: path.join(root, 'frontend'), env: { PATH: process.env.PATH, HOME: process.env.HOME, TMPDIR: process.env.TMPDIR || '/tmp', NODE_ENV: 'production', NEXT_TELEMETRY_DISABLED: '1', PUBLIC_APP_URL: `http://127.0.0.1:${frontendPort}`, BACKEND_URL: `http://127.0.0.1:${fixturePort}`, NODE_OPTIONS: '--max-old-space-size=384' }, stdio: ['ignore', 'pipe', 'pipe'],
});
let logs = '';next.stdout.on('data', b => { logs += b; });next.stderr.on('data', b => { logs += b; });
let browser; const captures = [], errors = [], external = []; let frameIndex = 0;
const origin = `http://127.0.0.1:${frontendPort}`;
async function ready() {
  for (let i = 0; i < 100; i++) {
    if (next.exitCode !== null) throw Error('Isolated frontend exited: ' + logs);
    try { const response = await fetch(origin + '/login'); if (response.ok) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  } throw Error('Isolated frontend did not become ready');
}
async function annotate(page) {
  await page.addInitScript(() => {
    const apply = () => {
      if (document.getElementById('synthetic-demo-ribbon')) return;
      const style = document.createElement('style'); style.textContent = `
        body { padding-top:36px !important; }
        .sidebar { top:36px !important; height:calc(100dvh - 36px) !important; }
        .mobile-toggle { top:50px !important; }
        #synthetic-demo-ribbon { position:fixed;left:0;right:0;top:0;height:36px;z-index:2147483647;display:flex;align-items:center;justify-content:center;background:#d7f7fb;color:#092c33;font:700 11px/1.3 Arial,sans-serif;letter-spacing:.08em;box-shadow:0 1px 0 #69ddeb;white-space:nowrap; }
        @media(max-width:500px) { #synthetic-demo-ribbon {font-size:9px;letter-spacing:.02em;} }
      `;
      document.head.append(style);
      const ribbon = document.createElement('div');ribbon.id = 'synthetic-demo-ribbon';ribbon.setAttribute('role', 'note');
      ribbon.textContent = 'SYNTHETIC UI DEMO · PAPER ONLY · NO LIVE DATA / ORDERS';document.body.prepend(ribbon);
    };
    globalThis.__quantixShowcaseApply = apply;
  });
}
async function stable(page) { await page.waitForTimeout(500); await page.evaluate(() => globalThis.__quantixShowcaseApply()); await page.locator('#synthetic-demo-ribbon').waitFor(); }
async function shot(page, name) {
  await stable(page);const file = path.join(output, name + '.png');await page.screenshot({ path: file, animations: 'disabled' });
  const viewport = page.viewportSize();const bodyOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  if (bodyOverflow) throw Error('Horizontal page overflow: ' + name);
  captures.push({ file: name + '.png', width: viewport.width, height: viewport.height, body_overflow: bodyOverflow, synthetic_label_visible: await page.locator('#synthetic-demo-ribbon').isVisible() });
}
async function frame(page, repeats = 1) { await stable(page); for (let i = 0; i < repeats; i++) await page.screenshot({ path: path.join(framePath, String(frameIndex++).padStart(3, '0') + '.png'), animations: 'disabled' }); }
try {
  await ready(); browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--disable-dev-shm-usage'] });
  const context = await browser.newContext({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 1, reducedMotion: 'reduce' });
  const page = await context.newPage();page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (!['http:', 'https:'].includes(url.protocol) || url.origin === origin) return route.continue();
    external.push(url.origin);return route.abort('blockedbyclient');
  });
  await annotate(page);
  await page.goto(origin + '/overview', { waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: 'Command Center.' }).waitFor();
  await page.getByLabel('Portfolio scope').selectOption(fixtures.ids[2]);
  await shot(page, 'terminal-desktop');await frame(page, 2);
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Portfolio', exact: true }).click();
  await page.getByRole('heading', { name: 'Portfolio accounting' }).waitFor();
  await shot(page, 'portfolio-desktop');await frame(page, 2);
  await page.getByLabel('Paper portfolio').selectOption(fixtures.ids[3]);await frame(page, 2);
  await page.getByRole('button', { name: 'Archived', exact: true }).click();await frame(page, 2);
  await page.getByRole('button', { name: 'Current portfolios', exact: true }).click();
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Analytics', exact: true }).click();
  const lab = page.getByRole('region', { name: 'Strategy Lab', exact: true });await lab.waitFor();
  await lab.getByText('Common opening funding', { exact: true }).click();
  await lab.evaluate(element => scrollTo(0, element.getBoundingClientRect().top + scrollY - 64));await shot(page, 'strategy-lab-desktop');await frame(page, 2);
  await lab.getByLabel('Comparison end · UTC').fill('2026-01-03T03:04:05');
  await lab.getByRole('button', { name: 'Compare strategy portfolios' }).click();
  const comparison = lab.getByRole('region', { name: 'Recorded forward paper comparison' });await comparison.waitFor();await comparison.evaluate(element => element.scrollIntoView({ block: 'start' }));await page.evaluate(() => scrollBy(0, -52));
  await shot(page, 'comparison-desktop');await frame(page, 3);
  await page.setViewportSize({ width: 390, height: 844 });await page.goto(origin + '/portfolio', { waitUntil: 'networkidle' });
  await shot(page, 'portfolio-mobile');
  await context.close();
  if (errors.length || external.length) throw Error(JSON.stringify({ errors, blocked_external_origins: external }));
} finally {
  if (browser) await browser.close();next.kill('SIGTERM');await transport.close();
  await new Promise(resolve => { if (next.exitCode !== null) resolve(); else { next.once('exit', resolve); setTimeout(() => { next.kill('SIGKILL'); resolve(); }, 3000).unref(); } });
}
const fixturesHash = createHash('sha256').update(await readFile(path.join(own, 'showcase-fixtures.cjs'))).digest('hex');
await writeFile(path.join(output, 'capture-report.json'), JSON.stringify({
  captured_utc: new Date().toISOString(), synthetic_ui_only: true, live_market_data: false,
  database_used: false, financial_engine_executed: false, actual_orders_or_telegram: false,
  source_build_id: (await readFile(path.join(root, 'frontend/.next/BUILD_ID'), 'utf8')).trim(),
  fixture_sha256: fixturesHash, captures, frame_count: frameIndex, page_errors: errors,
  blocked_external_origins: external, requests: transport.requests,
}, null, 2) + '\n');
console.log(JSON.stringify({ captures: captures.length, frames: frameIndex, page_errors: errors, output }));
