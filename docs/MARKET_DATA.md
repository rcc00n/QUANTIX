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

