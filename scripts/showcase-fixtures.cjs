/**
 * Explicit SYNTHETIC UI-only responses for portfolio documentation.
 * No exchange, database, Telegram, financial-engine or order-placement connection.
 * Shapes follow frontend/e2e/terminal.spec.ts and strategy-lab.spec.ts.
 */
const http = require('node:http');

const NOW = '2026-01-02T03:04:05.123456Z';
const ids = [1, 2, 3, 4].map(n => `90000000-0000-4000-8000-${String(n).padStart(12, '0')}`);
const names = ['Liquid Arbitrage', 'Anomaly Hunter', 'Hybrid Optimizer', 'Hold Control — No trading'];
const profiles = ['LIQUID_ARBITRAGE', 'ANOMALY_HUNTER', 'HYBRID_OPTIMIZER', 'NO_TRADING'];
const group = {
  id: '93000000-0000-4000-8000-000000000001',
  proposal_id: '94000000-0000-4000-8000-000000000001', started_at: NOW,
  allocation: {
    target_trade_cad: '250', reserve_cad: '500', unallocated_cad: '0',
    allocations: [
      { exchange: 'synthetic_a', asset: 'fiat:CAD', quantity: '5000', cad_value: '5000', cad_rate: '1' },
      { exchange: 'synthetic_b', asset: 'asset:SOL', quantity: '50', cad_value: '5000', cad_rate: '100' },
    ],
    coverage_by_size: ['50', '100', '250', '500'].map(size => ({
      trade_amount_cad: size, effective_minimum_cad: '50', considered_routes: 4,
      funded_alternative_routes: 2, simultaneous_funded_routes: 1, profitable_funded_routes: 0,
    })),
    funded_routes: [{ route_id: 'synthetic-sol-route', base_asset: 'asset:SOL',
      buy_exchange: 'synthetic_a', sell_exchange: 'synthetic_b',
      maximum_funded_acquisition_cad: '500', net_profit_cad: '-0.25' }],
  },
};
const sessions = ids.map((id, i) => ({
  id, name: names[i], status: i === 3 ? 'PAUSED' : 'ACTIVE', classification: 'RESEARCH',
  initial_equity_cad: '10000', started_at: NOW, strategy_version_id: '91000000-0000-4000-8000-000000000001',
  mode: 'RESEARCH_SHADOW', settings: { strategy_lab_profile: profiles[i], min_buy_capital_cad: '50', trade_amount_cad: '500' },
  lab_cohort: group,
}));
const archived = { ...sessions[0], id: '92000000-0000-4000-8000-000000000001',
  name: 'SYNTHETIC archived predecessor', status: 'ARCHIVED', archived_at: NOW, lab_cohort: null };
const zero = {
  initial_equity_cad: '10000', equity_cad: '10000', net_simulated_pnl_cad: '0',
  realized_trading_pnl_cad: '0', realized_fx_pnl_cad: '0', unrealized_pnl_cad: '0',
  fees_cad: '0', expenses_cad: '0', portfolio_return_pct: '0',
  no_trading_benchmark_cad: '10000', strategy_excess_cad: '0', capital_utilization_pct: '0',
  available_capital_cad: '10000', drawdown_pct: '0', reconciliation_delta_cad: '0',
  as_of: NOW, valuation_status: 'COMPLETE_FRESH', equity_reconciled: true,
  limitations: ['SYNTHETIC UI contract fixture — no journals or fills were executed.'],
  balances: [
    { exchange: 'synthetic_a', asset: 'fiat:CAD', quantity: '5000', available_quantity: '5000',
      reserved_quantity: '0', cost_basis_cad: '5000', market_value_cad: '5000', unrealized_pnl_cad: '0',
      cad_rate: '1', valuation_status: 'SYNTHETIC' },
    { exchange: 'synthetic_b', asset: 'asset:SOL', quantity: '50', available_quantity: '50',
      reserved_quantity: '0', cost_basis_cad: '5000', market_value_cad: '5000', unrealized_pnl_cad: '0',
      cad_rate: '100', valuation_status: 'SYNTHETIC' },
  ],
};
const metrics = { counts: { MISSED: 2 }, completed_simulations: 0, missed_simulations: 2,
  partially_completed: 0, failed_simulations: 0, expired_simulations: 0, scheduled_simulations: 0,
  win_rate_pct: null, completed_with_available_cad_pnl: 0, maximum_drawdown_pct: '0',
  by_route: [], by_asset: [], by_strategy: [], by_reaction_delay_ms: [],
  failure_reasons: { SYNTHETIC_INVENTORY_MISSING: 2 } };

function readOnlyComparison() {
  return {
    schema_version: 'quantix-strategy-lab-v1', kind: 'FORWARD_PAPER_OBSERVED', paper_only: true,
    period: { start: NOW, end: '2026-01-03T03:04:05.123456Z' }, initial_capital_cad: '10000',
    rows: sessions.map((s, i) => ({
      session_id: s.id, name: s.name, profile: profiles[i], strategy_version_id: s.strategy_version_id,
      strategy_version: 1, started_at: NOW, initial_capital_cad: '10000',
      opening: { as_of: NOW, recorded_at: NOW, equity_cad: '10000', benchmark_cad: '10000', valuation_status: 'COMPLETE_FRESH', equity_reconciled: true },
      closing: { as_of: '2026-01-03T03:04:05.123456Z', recorded_at: '2026-01-03T03:04:05.123456Z',
        equity_cad: i === 1 ? null : '10000', benchmark_cad: '10000',
        valuation_status: i === 1 ? 'PARTIAL' : 'COMPLETE_FRESH', equity_reconciled: i === 1 ? null : true },
      valuation_status: i === 1 ? 'PARTIAL' : 'COMPLETE_FRESH',
      completed_simulations: 0, partially_completed: 0, failed_simulations: 0,
      missed_simulations: 0, expired_simulations: 0, realized_trading_pnl_cad: '0', realized_fx_pnl_cad: '0',
      fees_cad: '0', expenses_cad: '0', unrealized_pnl_cad: i === 1 ? null : '0',
      portfolio_return_pct: i === 1 ? null : '0', benchmark_return_pct: '0', excess_return_pct: i === 1 ? null : '0',
      strategy_excess_cad: i === 1 ? null : '0', drawdown_pct: i === 1 ? null : '0',
      capital_utilization_pct: '0', inventory_exposure_cad: i === 1 ? null : '5000', largest_exchange_share_pct: '50',
      retained_checkpoint_count: 2, complete_checkpoint_count: i === 1 ? 1 : 2, ledger_reconciled: true,
      limitations: i === 1 ? ['SYNTHETIC missing FX checkpoint; unknown total stays unavailable.'] : ['SYNTHETIC illustrative contract data; no executed fills.'],
    })), limitations: ['SYNTHETIC UI fixture; this is not an executed forward-paper result.'],
  };
}

async function createFixtureServer({ port = 3372 } = {}) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) { body += chunk; if (body.length > 32768) { res.writeHead(413).end(); return; } }
    const url = new URL(req.url, `http://127.0.0.1:${port}`);
    const p = url.pathname.replace(/^\/api\/v1/, '');
    requests.push({ method: req.method, path: p });
    res.setHeader('Content-Type', 'application/json'); res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Quantix-Synthetic-Demo', 'true');
    const send = value => res.end(JSON.stringify(value));
    if (!['GET', 'HEAD'].includes(req.method) && !(req.method === 'POST' && p === '/analytics/strategy-lab/compare')) {
      res.statusCode = 405; return send({ detail: 'SYNTHETIC UI demo is read-only. No mutation is implemented.' });
    }
    if (p === '/auth/me') return send({ id: ids[2], username: 'SYNTHETIC_DEMO', mode: 'paper' });
    if (p === '/system/status') return send({ mode: 'paper', stage: 7, database: 'SYNTHETIC_FIXTURE', schema_revision: '0010_notifications', services: {}, counts: { exchanges: 2, opportunities: 0, paper_sessions: 4 } });
    if (p === '/system/events') return send([]);
    if (p === '/paper/sessions') return send({ items: url.searchParams.get('scope') === 'archived' ? [archived] : sessions, total: 4 });
    if (/^\/paper\/sessions\/[^/]+\/portfolio$/.test(p)) return send(zero);
    if (/^\/paper\/sessions\/[^/]+\/metrics$/.test(p)) return send(metrics);
    if (p === '/paper/trades') return send({ items: [], total: 0, limit: 25, offset: 0 });
    if (/^\/paper\/sessions\/[^/]+\/daily$/.test(p)) return send({ items: [] });
    if (p === '/strategies') return send({ current: { id: ids[2], version: 1, settings: {} }, presets: {}, reaction_delays: [5, 15, 30, 60, 120] });
    if (p === '/reports/exports/current') return send({ job: null });
    if (p === '/analytics/strategy-lab/compare') return send(readOnlyComparison());
    if (/^\/analytics\/sessions\/[^/]+\/series$/.test(p)) return send({ items: [
      { ...zero, as_of: '2026-01-01T03:04:05.123456Z' }, zero,
    ], sampling: 'SYNTHETIC_UI_FIXTURE', available_count: 2 });
    if (/^\/analytics\/sessions\/[^/]+$/.test(p)) return send({ schema_version: 'analytics-v1', classification: 'RESEARCH',
      period: { timezone: 'America/Edmonton' }, metrics, portfolio: zero, by_trade_size: [], strategy_versions: [],
      reconciliation: { delta_cad: '0' }, limitations: ['SYNTHETIC browser-only contract fixture.'] });
    if (p === '/terminal/overview' || p === '/terminal/health') return send({ as_of: NOW,
      connected_exchanges: 2, venue_groups: 2, monitored_markets: 4, compatible_intersections: 2,
      scanner: { healthy: true, clock: { trusted: true, host_healthy: false }, metrics: { verified_market: 0, research_only: 1 } },
      paper: { healthy: true }, feeds: [], events: [], recent_opportunities: [], recent_simulations: [],
      warnings: ['SYNTHETIC fixture transport. No live exchange data or orders.'], collector: { applied_settings_version: 1 }, telemetry: {} });
    if (p === '/notifications/status') return send({ paired: false, worker: null, pairing: null,
      pending_challenges: [], telegram_enabled: false, delivery_active: false,
      providers: { telegram: { available: false }, push: { enabled: false }, voice: { enabled: false } }, queue_counts: {} });
    if (p === '/paper/funding/catalog') return send({ exchanges: [], assets: [] });
    res.statusCode = 404; return send({ detail: 'No SYNTHETIC fixture for this read operation.' });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
  return { server, requests, close: () => new Promise(resolve => server.close(resolve)) };
}
module.exports = { createFixtureServer, ids, NOW };
