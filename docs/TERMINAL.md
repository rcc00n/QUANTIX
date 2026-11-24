# QUANTIX terminal — Stage 5

All views require the existing authenticated opaque session. Financial scalars stay
exact decimal strings. Charts convert to browser numbers only for display and retain
exact values in tooltips; they never determine trading or accounting outcomes.

## Working surfaces

- Command Center joins measured feed/scanner/paper health with one explicitly selected
  portfolio, benchmark, recent public projections and classified virtual outcomes.
- Radar filters and sorts projected quantities, CAD profit/return, liquidity and
  confidence. Advanced filters are secondary to the route list on mobile. Evidence
  drawers preserve timestamps, depth, fees, FX, inventory assumptions and lifecycle.
- Paper Trading and Portfolio retain immutable accounting and separate research
  capital. The trade inspector joins original detection, delay, independent legs,
  fills and journal entries. Historical replay is read-only and reports absent
  delayed evidence instead of inventing execution.
- The funding planner explains exact missing purchase currency and sell inventory.
  Independent research experiments consume their entire stated CAD initial capital;
  resets create a new experiment and preserve history. See FUNDING.md.
- Analytics uses retained checkpoints, full-period SQL aggregates and scope-aware
  ledger reconciliation. Completed-trade statistics remain unavailable with zero
  completed known-CAD samples. Holding gains/losses are distinct from trading results.
- Settings saves immutable strategy and terminal preference versions. Market changes
  apply on the next collector allocation tick (at most60seconds); saved/applied
  versions are visible. Notifications remain configuration-only, delivery inactive
  through Stage5. AI Insights remains disabled; no external model is called.
- System Health shows actual clock/source uncertainty, feed modes/quality/freshness,
  scanner/recheck backlog, coordinator health, schema, bounded request latency and
  measured API/collector process RSS/CPU. Other service telemetry stays unavailable
  when no host agent/Docker socket is granted. It never fabricates host measurements.
- Stage 10 adds a per-asset coverage view from the bounded current collector selection.
  Coinbase adapter aliases share a venue group. Same-quote directions require verified
  asset identities and explicitly active listings. Connected venues use recent healthy
  or degraded books. REST/WS source, quote freshness and synchronized WS coverage are
  reported separately. Tracked unexpired scanner projections provide Decimal buy size,
  positive after-cost research counts and timing sample sizes. The panel labels unavailable
  values and links to the session-scoped Funding Planner for actual portfolio inventory.
  This projection runs on the existing authenticated status read with no extra database
  query, exchange request or scanner job.
- Analytics includes Strategy Lab for two to four independent research portfolios with
  equal initial capital and one UTC comparison window. Results use recorded forward paper
  activity and each portfolio's original holdings benchmark. The comparison displays
  period outcomes and realized amounts, closing unrealized exposure, risk and valuation
  limits, and the strategy version and timestamp of each retained boundary checkpoint.
  Running a comparison is read-only and on demand. Historical replay remains in the
  separate Paper Trading evidence view.
- System Health displays the collector's sustained freshness, scanner progress,
  WebSocket synchronization and UTC checks. Pending checks show how long the observed
  transition has been under confirmation. Accepted degradations and recoveries appear
  in recent operational events. The page reads the singleton Telegram worker status
  once per minute while visible and shows delivery policy, health and reported
  heartbeat separately.

## Efficient authenticated updates

Bounded polling is used instead of introducing SSE connections or a broker:15seconds
for operational summaries/portfolios,30seconds for analytics,60seconds for catalogs,
with existing5–10second book/Radar views. Hidden tabs pause, one request batch is in
flight, cleanup aborts requests and reconnect backs off to at most60seconds. Errors
mark retained values stale;401returns to login. Backend public-runtime singleflight
cache is5seconds and never stores user portfolio data.

Default Radar comparisons use the most recent500retained candidates. Custom capital
uses a disclosed50candidate window and the configured live search bound(default128).
Only one sizing request runs per API process; contention returns429/Retry-After.
Depth/fee/FX calculation quality is unchanged and uncertified sizing remains explicit.
On-demand details retain the4096evaluation search bound. CPU work runs outside the
API event loop. Private collector list summaries omit bulky raw books only from the
transport; detail and immutable database evidence remain complete. These limits do
not promise every short-lived discrepancy will be captured.

## Exact market links

Only individually checked complete official URLs are linked, without interpolating
symbols or assuming eligibility. Checked2026-11-22 against the matching official
Kraken Pro spot page titles: [BTC/CAD](https://pro.kraken.com/app/trade/btc-cad),
[ETH/CAD](https://pro.kraken.com/app/trade/eth-cad),
[SOL/CAD](https://pro.kraken.com/app/trade/sol-cad),
[BTC/USD](https://pro.kraken.com/app/trade/btc-usd),
[ETH/USD](https://pro.kraken.com/app/trade/eth-usd).
Other markets show unavailable links until verified. Opening a screen never
establishes that a projected spread still exists.

## Financial fixes and compatibility

Paper calculation version paper-depth-v1.1.0 floors internally derived reaction/
slippage-adjusted budgets to18decimal places before strict optimizer validation. Planned reservations
match the simulator's separate upward rounding of notional and quote/base fees, so
long-precision quantities remain fundable without understated debit requirements.
This fixes rejection of repeating decimals at the default nonzero slippage; it does
not relax inputs or increase the budget. Full delayed two-leg/idempotent ledger
regression covers this case. Existing source evidence and prior versions remain
immutable. Optional maximum_slippage_bps limits each modeled depth VWAP's adverse
impact from the contemporaneous top quote; exceeding it fails that independent leg.
An unset additional cap preserves old pinned strategy behavior. Fees are separate.

Market preferences may tighten source freshness and select exchanges/assets/quotes;
fiat/stablecoin identities remain distinct. Disabled venues stop subsequent public
polls and streams; late deselected responses do not re-enter the current cache or
scanner and retain an operational event. Coverage changes can remove future evidence
for existing work, which must expire/fail honestly; strategy versions never mutate.
Suspicious books are retained even when optional anomaly exploration is disabled.

## Reporting and deployment boundaries

Export for ChatGPT streams an authenticated self-contained archive, with exact raw
records, opening dependencies, definitions, selected period/scope, limitations and
SHA256manifest. See REPORTING.md. No test dataset enters live history. All real-trade
records remain separate; no real-order or paid provider call is enabled.

Local entry point: http://localhost:3100. Production operation is separate from
source import and requires the deployment and recovery gates.


Same-UTC lifecycle events use the known CANDIDATE→ELIGIBILITY_CHECK→SCHEDULED→
RECHECKING→EXECUTING→terminal ordering for presentation and historical cutoff ties.
Stored timestamps/IDs/provenance never change. Each simulated leg explicitly shows
its venue and native base/quote denomination; mixed USDT/EUR prices must not appear
as if they share a quote currency. Unknown fee units remain unknown.


## Final Stage6 operational extension

Signals&Alerts now uses authenticated /notifications status, challenge/approval/
revocation, preview/history/events/decisions, explicit test, pause/resume and ack/
cancel routes. The bot is connected; the owner remains unpaired, so delivery is
inactive. Notification settings are real versioned controls rather than Stage5-only
placeholders. Independent minimum executable BUY capital defaults to50CAD and offers
10/25/50/100/250/500/1000/custom presets. Paper controls have their own pinned minimum.
See NOTIFICATIONS.md; paid providers remain disabled.

AIInsights reads actual owned portfolio or retained opportunity evidence through
/ai/status and /ai/explain. Deterministic explanations work with externalAI disabled.
No arbitrary prompt, model-authored accounting or recommendation enters financial
logic. Size diagnostics compare seven targets from C$50 to C$5,000 and show the
best size found within the selected budget. Each row reports net CAD, costs, both
paper reserves and binding constraints. An uncertified search identifies the best
evaluated size.

Telegram opportunity deep links require dashboard authentication and reopen retained
evidence after login. Return paths are restricted to known local dashboard routes;
external/open redirects are rejected. Opening a historical route never rechecks or
promises current profitability. The owner explicitly chooses recheck.

