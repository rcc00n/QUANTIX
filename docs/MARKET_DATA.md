# QUANTIX public market-data pipeline

The collector uses strict source/receipt ordering, RAW timing, explicit freshness
classes, bounded fair event-driven processing and live connector status. Read
[timestamp definitions](MARKET_DATA_TIMESTAMPS.md) before changing these paths.

Stage 2 collects public data only. It has no order-placement methods, trading keys,
simulated fills or paid feeds. Stage 3 adds a read-only research scanner. Its module identifier
arbiq is intentionally retained. CCXT is locked to the version in backend/uv.lock.

## Process and API boundary

A separate `collector` Compose service owns one PostgreSQL advisory session lock.
A second collector cannot take over that database. Startup creates an audited run
with a public configuration snapshot, marks abandoned prior runs, and discovers
adapter capabilities from the installed CCXT version. Natural keys make asset/market
upserts repeatable; balances and financial history are never reset.

Each exchange has one persistent read-only CCXT async instance, a pooled aiohttp
session, a serial request gate and CCXT's weighted rate limiter. A global semaphore
bounds exchange requests. Startup/discovery errors isolate that exchange. Timeouts,
rate limits, auth-required public products, access denial, malformed responses and
unsupported requests have separate sanitized reason codes. Retries use exponential
backoff, jitter and a cap. HTTP Retry-After is honored; 429/access-denied/auth-required
failures pause the whole exchange, including other market schedules. No credentials
are supplied to a public endpoint that requests authentication.

The collector's bounded memory cache is served privately on port 8001. FastAPI
verifies dashboard sessions and forwards to that cache using a random internal
Bearer token. Unauthenticated users cannot access books through either API. When
live cache access fails, FastAPI may return the last persisted book, clearly marked
as a fallback with confidence downgraded and timestamps retained. No data is created
for an empty/unavailable market. Backend/dashboard operation survives collector outages.

## Discovery, filters and identity

- Discover explicit spot, non-contract markets dynamically. Where CCXT supports a
  product-type selector, restrict discovery to spot to avoid derivative traffic.
- Persist all normalized discovered spot metadata, including inactive/unverified
  status. Monitor only the bounded selection that passes configuration filters.
- Filters: exchange allow/exclude, base asset list (empty means all), quote list,
  active-only and allow-unknown-active switches, minimum native quote volume, CAD
  preference, minimum visible book notional/depth, and freshness.
- Select distinct base assets before redundant quote variants. CAD is preferred;
  BTC/ETH/SOL/XRP/LTC/USDC/USDT are priorities, not the entire market universe.
- Bulk ticker volume is optional and requested only for selected known single-call
  public endpoints. Missing volume is unknown, not zero; a positive minimum-volume
  filter excludes unknowns. Liquid markets get the faster interval. Unknown/less
  active markets get the slower interval. Full-book REST endpoints get slower polling.
- Volume and visible notional use **native quote units**. A threshold of 10000 CAD
  is not silently equated to 10000 USD, USDC or USDT. Stage 3 adds explicit executable FX evidence; discovery liquidity thresholds remain native quote units.
- Curated assets and fiat currencies have explicit canonical keys. Wrapped, staked,
  bridged and synthetic names/symbols are isolated. Other unverified tickers remain
  exchange-scoped even if their display symbols match. Funding-network compatibility
  still needs verification in later risk/rebalancing stages.
- `markets.native_symbol` retains the Stage 1 column name and stores the CCXT unified
  symbol used for requests; metadata.native_id preserves the exchange-native ID.
- Metadata changes cannot replace an existing market's base/quote asset FKs. A
  conflict quarantines the listing, records an event and preserves old provenance.
- During one discovery of one venue, normalized markets share immutable identities
  resolved from that discovery's currency table. Each rediscovery resolves identities
  against its own currency table. The collector releases bulk tickers after extracting
  liquidity and invalidates the prior native archive after it loads a new catalog.
  A selection update looks up the requested active symbols and writes rows whose
  monitored flag changes, preserving other metadata attributes.
- The collector loads persisted timestamp watermarks and prepares the native
  adapter before publishing a changed market list and its schedules together.
  An in-flight response from a retired schedule cannot update live books or
  connector status, even if the same market is selected again.

## Exact decimals and validation

Public JSON decimal numbers are decoded to Decimal, not float. CCXT's safe_string
hook is extended for Decimal currency metadata. Its L2 level parser is replaced
because the locked version's default uses safe_float independently of its number
setting. Exchange-specific accepted depth limits are mapped before requests. The
normalizer rejects floats, non-finite/negative/zero prices/quantities, excessive
precision and values outside NUMERIC(38,18). It never silently rounds a price.

Each observation retains exchange timestamp (nullable), UTC request/ingestion times,
source, nonce where supplied, bounded raw levels and SHA-256 content hash. Missing
exchange timestamps use explicitly identified ingestion time for the legacy
NormalizedOrderBook.observed_at field, and remain DEGRADED. Kraken REST timestamps
are derived from the newest native price-level update, not invented as server time.
Bitfinex explicitly requests aggregated P0/L2 books; the adapter default R0/L3
contains duplicate price levels and is unsuitable for the L2 contract. Its CCXT-
generated local timestamp is removed because the public response supplies none.
REST duration includes CCXT throttling and parsing but excludes scheduler waiting;
stream updates have no fabricated REST latency.

Validate ordering, duplicate prices, crossed/locked sides, positive levels, minimum
depth, metadata/identity, UTC timestamps, future skew, source age, ingestion age and
clock reversals/jumps. ClockGuard compares monotonic elapsed time with wall-clock
movement and requires five stable checks to recover after drift. A local per-run
ingestion_index orders cache/persistence writes independently of wall-clock movement;
it is not an exchange sequence number and never bypasses timestamp quality gates.

| State | Meaning |
| --- | --- |
| HEALTHY | Structural, timestamp, identity and freshness checks passed |
| DEGRADED | Usable observations with explicit uncertainty, e.g. no exchange timestamp or unknown active status |
| STALE | Source or ingestion age exceeds the configured limit |
| SUSPICIOUS | Crossed/invalid/unsorted book, clock/symbol anomaly, or extreme temporal/reference discrepancy; inspect evidence |
| UNAVAILABLE | No usable two-sided book or disconnected/unavailable source |

Confidence changes also update the legacy normalized quality tag, so quarantined or
stale data cannot retain a valid tag at that interface boundary. All book DTOs retain actionable=false. HEALTHY data alone is not an arbitrage opportunity.
Operational exchange health is separate from individual-book confidence. API failure
immediately downgrades cached confidence, retains the last real record and adds the
error; old values are never presented as a successful new fetch.

## WebSocket recovery

Kraken v2 uses public snapshots and incremental updates. Every message is applied
atomically to price levels, zero quantities delete levels, depth is truncated to the
subscription scope, and CRC32 over the top ten asks/bids is checked using exact
strings. Updates without a snapshot, checksum mismatch, unknown symbols, rejected
subscriptions, idle timeout or disconnect trigger reconnection and a new snapshot.
REST continues as fallback when stream updates are unavailable/stale.

Kraken v2 has checksums rather than contiguous sequence IDs. SequenceGuard supports
feeds that explicitly provide contiguous sequences and is tested with synthetic gaps;
it is **not** falsely applied to REST nonces or represented as a live Kraken sequence
feed. Native Coinbase Exchange and OKX protocols provide additional bounded depth
slots, each with protocol-specific integrity checks and REST fallback. Selection is
independent of the wider discovery catalog. Other installed CCXT Pro capabilities
are recorded as advertised, not claimed as implemented or live-verified coverage.
See [WEBSOCKET_ARCHITECTURE.md](WEBSOCKET_ARCHITECTURE.md) for current transports.

## Anomaly confirmation

The cache checks temporal jumps and fresh independent same-canonical-asset/same-quote
reference midprices. USD and stablecoins cannot cross-confirm each other. Coinbase
and Coinbase Exchange count as one venue. At least two independent references are
required for confirmation. Outliers remain visible with raw evidence and a confidence
flag. An anomalous record is quarantined across updates until a fresh REST recheck
and agreeing references resolve it. A repeatable price that disagrees with references
remains SUSPICIOUS; Stage 3 must evaluate that evidence rather than assuming profit.

Rechecks are automatic for suspicious books and manually available in the explorer.
A bounded queue (32), per-market 30s cooldown, existing exchange backoff and global
concurrency/rate limits apply. Recheck responses say queued, not confirmed. Insufficient
references, disagreement and recovery have explicit verification statuses.

## Bounded persistence and retention

- latest_market_books: one mutable fallback row per market; old rows expire after 24h.
- market_data_samples: one observation per market/time bucket (default 300s), retained
  24h by default, maximum seven days. Unique buckets prevent restart duplicates.
- market_quality_events: bounded raw/confirmation evidence, deduplicated per minute,
  retained seven days by default, configurable up to 90 days.
- Both transient history tables have a configurable global row budget, enforced by
  hourly cleanup after time retention (default 100000 rows per table). Temporary
  overshoot between cleanup passes is bounded by the configured collection budget.
- Unusual samples may also receive an immutable order_book_snapshots record, at most
  one per market/hour and 24 total/hour by default. Repeated content is deduplicated.
  These low-frequency evidence records remain auditable; no financial references are
  pruned. Stage 3 will explicitly pin trade/opportunity evidence when needed.
- Durable system_events record startup/shutdown, discovery, connectivity changes and
  distinct errors, with repeated error deduplication. They contain no tokens or secrets.
- Stage 2 cleanup never deletes the Stage 1 immutable ledger, market observations,
  fills, valuations, fee/config versions, user records, actual trades or snapshots.

## Configuration and diagnostics

Environment nested prefix: COLLECTOR__. Arrays are JSON arrays; decimal thresholds
are decimal strings/numeric environment text. Restart the collector after changes.
Important settings are listed in .env.example; all options and bounds live in
arbiq.collector.config. COLLECTOR__ENABLED=false leaves a disabled, network-inert
collector endpoint. To stop the service entirely use docker compose stop collector.

`make live-check` reads actual current-run records, reports separate adapter/venue
counts, discovered and monitored listings, available books, source coverage, quality,
latency and freshness. No mock data enters the application database. To verify live
browser behavior use QUANTIX_LIVE_E2E=1; CI's synthetic backend tests never imply that
a public exchange was connected.

