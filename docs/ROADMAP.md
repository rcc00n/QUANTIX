# QUANTIX roadmap and subsystem boundaries

This roadmap describes implementation stages and their acceptance requirements.
The import contains the current accepted source, including later funding, transport,
Strategy Lab and reliability improvements. Operational acceptance evidence is private;
this roadmap does not certify a particular running deployment. See
[history import](HISTORY_IMPORT.md) and [architecture](ARCHITECTURE.md).

| Stage | Deliverable | Dependencies | Acceptance gate |
| --- | --- | --- | --- |
| **1. Foundation** | Permanent instructions, modular FastAPI/Next.js monorepo, PostgreSQL schema/migrations, session authentication, local Compose, financial/report contracts, tests | Local Docker; no exchange or AI credentials | Stack healthy, migrations applied, private UI/API protected, contracts and integrity checks pass |
| **2. Public market data** | CCXT read-only adapters; discovery for about 10–20 exchanges as supported; canonical assets/pairs; rate limiting/backoff; normalized timestamped books; selective native WebSockets; connectivity and quality events | Stage 1 contracts, persistence and settings | Live public data with provenance; stale/crossed/incomplete data flagged; symbol collisions handled; outage/reconnect tests |
| **3. Quantitative opportunities & risk** | Executable depth evaluation; CAD FX; exchange-specific fee versions; precision/minimum limits; capital constraints; latency/slippage and rebalancing reserves; ranked opportunities with explanations from rules | Validated Stage 2 books and metadata | Deterministic financial tests, replay fixtures, conservative net estimates, no inference of executable liquidity from tickers |
| **4. Paper execution & portfolio** | Versioned strategies; isolated validated/research capital; continuous numerical analytics and Edmonton daily snapshots; independent virtual exchange balances; initial deposits; configurable manual reaction delay; fresh-book re-evaluation; two-leg fills/partial/missed/failed states; atomic balanced journals; inventory/cost basis; transfers/rebalancing expenses; valuations | Stage 3 opportunities and risk; Stage 1 ledger | Idempotent simulation, concurrency/overspend tests, ledger reconciliation, failure provenance, clear separation from real user trades |
| **5. Analytics & analytical export** | Extend Stage 4 numerical analytics and daily/benchmark interfaces; complete Export for ChatGPT ZIP; advanced dashboard, charts and visual analytics | Stage 4 accounting and evidence; v1 reporting contract | Reconcile exported records to ledger, stable CSV/JSON schema, correct periods/units, deterministic replay, no AI-authored substitute data |
| **6. Signals, Telegram & optional analysis** | Versioned alert rules, cooldown/deduplication, delivery retries and audit trail; nighttime emergency push/acknowledgment/escalation contracts and optional disabled voice; optional interchangeable NVIDIA explanatory provider | Stages 3–5; Telegram credentials only for enabled delivery; AI authorization separately | Alerts distinguished from detection/delivery; outages do not stop core processing; no paid AI without authorization; credentials masked |
| **7. VPS production release** | Authenticated public deployment; TLS/reverse proxy, secure cookies, firewall, least-privilege DB roles, backups and restore drill, retention/partition policy, resource budgets, monitoring and remote verification | All earlier stages; SSH/DNS/TLS access provided when needed | Authenticated HTTPS, internal DB, restore/recovery checks, load/soak tests, deployment and operator runbooks |

## Imported capabilities and open work

The source includes the seven core subsystems above, plus later native Coinbase
Exchange/OKX transports, bounded market allocation profiles, fee and execution-rule
provenance, executable sizing diagnostics, isolated Strategy Lab profiles, common
opening cohorts, exact shared-frame valuations and reliability/recovery tooling.
These are source capabilities, not a claim that every external venue or operating
condition has been independently approved.

Real-money execution has no enabled implementation. Paid push and voice are disabled;
external AI is optional and disabled by default. Unknown owner eligibility, account
fees, executable liquidity and missing valuation evidence remain explicit. Demonstrated
profitable paired execution, multi-day unattended storage runway and recurring
offsite recovery are not certified by this import. Future work must preserve those
boundaries and receive its own scope and acceptance evidence.

