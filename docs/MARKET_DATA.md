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

