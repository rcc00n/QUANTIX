# QUANTIX Stage 3 scanner

The scanner is a separate asyncio task inside the singleton collector process. It
shares the validated cache and bounded REST recheck scheduler, not an exchange
trading client. It never creates orders, fills, ledger entries or alerts. Stage 4
will manage virtual funding, execution delays and accounting.

## Coverage and scheduling

Discovery indexes canonical base/quote keys by independent venue group in linear
catalog time. Only actively monitored compatible markets are compared. Coinbase and
Coinbase Exchange share one venue group and never compare against each other.
Unknown, wrapped and synthetic identities remain isolated. Market selection reuses
Stage 2 filters and ranks CAD/conversion anchors, cross-venue coverage, configured
asset priority, quote preference and available native-quote volume. The default cap
is ten markets per adapter (configurable 1–50), not thousands of subscriptions.
Catalog-wide allocation is reconsidered every minute without rediscovering APIs.
The existing liquidity-adaptive refresh, weighted rate limits, retry/backoff,
concurrency and Kraken WebSocket controls remain in force.

Changed books coalesce by canonical base; conversion-book changes also revalue
related routes. The active index bounds comparisons to monitored markets. Metrics
separate discovered compatible keys, monitored keys, unordered venue intersections,
directed comparisons, evaluations and detections. They never equate discovered
listings with monitored books. Screening uses ask/bid prices, never last prices.

The scanner requests fresh REST evidence through the collector's existing queue32,
30-second per-market cooldown and exchange backoff. Rechecks never bypass rate
limits, imply execution or promise that a spread persisted. Extreme repeatable
outliers retain snapshots, independent references and classification; disagreement
alone does not erase a possible genuine dislocation. Invalid structure, stale source
or ingestion timestamps and disconnected sources prevent verified classifications.

## Financial inputs and confidence

See [FINANCIAL_ENGINE.md](FINANCIAL_ENGINE.md) for exact equations and optimizer
limitations. All financial arithmetic is Decimal with a private 78-digit context.
The scanner uses marketable taker assumptions, observed depth, precision/amount and
notional limits, separate native currencies, executable CAD conversions and explicit
slippage/rebalancing reserves. No depth extrapolation or stablecoin parity exists.
The buy budget excludes the separately reported pre-funded selling inventory;
both venue capital requirements and projected inventory changes are disclosed.

Default fee estimate per leg is max(0.6%, advertised public taker metadata). Account
fee tier, fee currency and future rebalancing expense are unverified. The scanner
assumes quote-currency fees and reserves 10bps slippage plus 10bps rebalancing, all
clearly labeled unverified. These configurable estimates are not exchange guarantees
or worst-case cost bounds. Fee versions preserve the policy and advertised value.
Unknown critical fees are never represented as zero or verified.

CAD is an identity conversion. Other quotes require fresh executable currency books
among CAD/USD/USDC/USDT/EUR, at most two hops, with acquisition asks and liquidation
bids, observed depth and conversion fees. Conversion timestamps, hashes, routes and
book evidence are pinned. Conversion uses public reference venues; actual funding
and account access are unverified. Missing/stale/insufficient conversion evidence
suppresses CAD profit. Native same-quote calculations may remain visible, explicitly
without an applied CAD budget. Crypto legs are not reused as FX depth. A depeg changes
observed conversion rates instead of being overridden with one USD.

Pre-funded calculations assume buying quote on one venue and selling base on the
other; they do not infer balances. Transfer scenario calculations require separately
supplied network/withdrawal/deposit/confirmation/timing assumptions, charges and a
future-price haircut. Live transfer results remain unavailable while those inputs
are unverified. Current sale prices are never guaranteed after a transfer delay.

## Lifecycle, persistence and API

DETECTED, RECHECKING, VERIFIED_MARKET, RESEARCH_ONLY, INVALIDATED and EXPIRED are
explicit states. Actionability is a separate flag. Every Stage 3 route has
`actionable=false`, enforced in PostgreSQL, because provincial/account eligibility
and pre-funded inventory remain unverified. Positive spreads with unknown fees or
clock/identity/market restrictions are research, never manufactured actionable
signals. Nonpositive after-cost estimates and invalid/stale books are invalidated.
No live route must be profitable for the engine to be working correctly.

The additive 0005 migration introduces mutable `arbitrage_routes` plus append-only
`arbitrage_evaluations` and `arbitrage_lifecycle_events`. Evaluations pin immutable
book snapshots and fee versions plus full FX, clock, metadata, reserve, optimizer
and calculation-version evidence. This avoids fabricating Stage 1 required FX or
strategy references for incomplete research. The original `opportunities` table
and all Stage1financial records remain intact. Stage4's separate paper coordinator
bridges eligible route/evaluation evidence into immutable versioned simulations.
The scanner itself never writes fills; see ACCOUNTING.md.

In-memory routes are bounded to500, evidence writes to30/minute, repeat evidence
normally to120seconds per route. Five negative samples/minute reserve capacity for positive anomalies. Comparisons
are time-sliced (up to512 comparisons or0.5s/cycle) and CPU optimization runs off the network event loop. Continuous scans cap additional sizing search at128 evaluations;
uncertified optima are explicitly flagged research. On-demand/replay calculations
retain the fuller4096-evaluation bound. Terminal transitions are queued within the same
budget and retried. Old terminal cache entries may be evicted after TTL; durable
history stays in PostgreSQL. Restart expires prior-run current routes and requires
new evidence. No financial evidence is deleted by Stage 2 retention. Long-running
production financial-evidence archival/partition policy remains Stage 7 work.

Live API views merge live projections with durable headers. If IPC/UTC verification
fails, retained data is labeled research/expired and cannot become verified. Budget
recalculation uses the original decision timestamp and pinned inputs, never future
books. List queries use a disclosed latest500 candidate window, max100 results/page,
exact Decimal ordering and bounded offsets. History and details retain evidence even
when no current route exists.

Stage 11 adds a tri-state research classification to each Radar projection. Newly
captured evaluations record the raw positive bid/ask screen. Cross-currency screens use
the captured directional FX evidence. The projected gross CAD
result describes the selected size before fees and reserves. Modeled depth feasibility
requires evidence of both quantities and both capital requirements, and executable CAD
positivity also requires a positive net result. Missing evidence is `null`. Live,
retained, stale and unverified freshness have separate labels. Source and receipt
skew assessments include capture uncertainty and use the current configured tolerance.
Source time is the venue's reported time. The original calculation and timestamps remain
available in its evidence.

Ranking uses exact net CAD by default. Ties use book quality, current freshness,
source and receipt synchronization, depth-constrained CAD capacity, return on buy
capital, receipt age, eligibility and fee verification, then stable route ID. Explicit
return and liquidity sort options retain their selected financial metric. The bounded
list ranks and retains the requested page in its existing projection worker. The
depth-constrained maximum is separate from the selected buy size and portfolio inventory.
Public fee evidence remains separate from account verification. Radar leaves session
inventory unassessed and links to the Funding Planner for the selected portfolio's
two-sided requirements and shortfalls. All routes remain PAPER ONLY and non-actionable.

Authenticated endpoints under `/api/v1/opportunities`:

- GET empty suffix: current/history, budget_cad, asset, exchange, status, sort,
  limit and offset. Budgets support custom positive CAD amounts up to1000000.
- GET /budgets: configured example budgets and default.
- GET /diagnostics: process, UTC/host health, coverage, latency, invalidation reasons.
- GET /{id}: retained/latest live cost breakdown, FX/fee/book/clock provenance and
  last100 lifecycle events, with optional budget_cad.
- POST /{id}/recheck: bounded fresh-book request, explicit queued/rate-limited result.

The BFF exposes only these authenticated routes. Internal collector IPC has a bearer
secret and no published host port. Engine diagnostics contain no credentials. The
Radar displays actual results or honest empty/loading/error states; estimates are
never realized P&L. Telegram, AI, paid providers and production hosting are unchanged.

