# QUANTIX — permanent engineering instructions

These instructions apply to every future session and all files in this repository. Read
`PROGRESS.md`, `docs/ROADMAP.md`, and relevant module documentation before continuing.
Complete only the requested stage; do not start the next stage automatically.

## Autonomous execution
- Work independently and make sensible engineering decisions.
- Do not interrupt development with clarification questions.
- Complete everything possible with available tools and access.
- If credentials, user intervention, or unavailable infrastructure are required,
  document the blocker and continue with other tasks.
- Ask all necessary questions together only at the end of the current task.
- Do not repeatedly request information already provided.
- Fix your own implementation errors and failing tests.

## Engineering quality
- Build real working software, not visual prototypes pretending to be functional.
- Use modular, maintainable architecture with clear separation of concerns.
- Use strong typing, validation, structured logging, and comprehensive error handling.
- Include tests for every critical financial calculation.
- Never fabricate exchange data, profits, or successful executions.
- Explicitly identify all test fixtures as synthetic data.
- Maintain documentation as the project evolves.
- Inspect existing project files before modifying or replacing them.
- Preserve completed functionality and avoid destructive operations.
- Use Python 3.12+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, Pydantic and asyncio;
  Next.js, React, TypeScript, Tailwind CSS and shadcn/ui for the frontend.
- Add dependencies only when justified. Prefer simple infrastructure for a modest VPS.

## Financial safety
- Operate in PAPER TRADING mode only. Never submit real exchange orders.
- Never enable real trading without separate explicit authorization and an implementation stage.
- Public exchange market-data APIs do not require trading credentials.
- Never assume an observed market price guarantees executable liquidity.
- Account for order book depth, fees, FX conversion, execution delay, slippage,
  and rebalancing costs in implemented simulations.
- Distinguish opportunities, signals/alerts, simulated executions, and real
  user-confirmed trades in storage, APIs, UI and reports.
- Never present estimated or simulated profit as realized real-world profit.
- Use Decimal and PostgreSQL NUMERIC for money, prices, rates and quantities;
  never binary floating point for financial calculations. Use UTC timestamps.

## Security
- Never hardcode credentials or API tokens. Use environment variables and `.env.example`.
- Keep `.env` and all secrets outside version control. Never log secrets.
- Authenticate private endpoints and the dashboard.
- Do not expose PostgreSQL or internal services publicly.
- Never expose a service to the internet without suitable authentication and TLS.
- Local development services must bind host ports to loopback only.

## Reporting and accountability
- Persist all meaningful domain events to PostgreSQL.
- Keep an auditable history of every virtual order and balance change.
- Track realized simulated P&L, unrealized P&L, fees, capital utilization and
  portfolio equity separately.
- Track missed and failed opportunities and their reasons.
- Preserve provenance needed to reconstruct every simulated outcome: source data,
  fee/configuration versions, FX assumptions, timing and calculation version.
- Support comprehensive stable exports inspectable without database access.
- Maintain `PROGRESS.md` after every development stage, including checks and blockers.
- Keep financial logic deterministic and independent of AI-generated text.
- Never silently modify applied migrations; add new migrations.
- Ledger entries are append-only; corrections use compensating entries.

## AI integration
- External AI is optional and never required for scanning, risk or paper trading.
- Prefer free, zero-cost functionality.
- Architect optional NVIDIA-hosted AI for later use; disabled by default.
- AI may explain signals, analyze completed results and summarize reports.
- AI must not invent market data or override quantitative decisions.
- Core functions remain operational when AI is unavailable.
- Never enable billable API usage without explicit authorization.

## Verification and stage boundaries
- Run appropriate tests, migrations, builds and health checks where available.
- Report executed checks separately from checks blocked by infrastructure.
- Unimplemented UI and APIs must say so; do not return fake performance or market data.
- A source import does not authorize deployment, server maintenance or shutdown.

## Product identity and Stage 2 invariants
- The official name is QUANTIX only. Never expand it or explain it as an acronym.
- Public deployment remains a separately authorized operational action.
- Preserve internal arbiq module/database/Compose identifiers to avoid destructive migrations.
- Public data accessibility, data confidence and Canadian eligibility are separate dimensions.
- Unverified exchange eligibility is never approved; public feed access never establishes eligibility.
- Suspicious books are retained with evidence and excluded from verified data until independently confirmed.
- Keep collectors in a separate process with bounded memory, concurrency and persistence.
- Retention may prune only explicitly transient, unreferenced market samples; never financial evidence.
- Owner username is quantix. Credentials stay in protected local secrets, never progress documents/logs.
- Stage 6 urgent notifications use America/Edmonton quiet hours; paid push/voice are disabled pending explicit authorization.

## Stage 3 quantitative invariants
- Read docs/ARBITRAGE.md, docs/FINANCIAL_ENGINE.md and docs/CLOCK.md before changing
  scanning, sizing, freshness, fee or conversion logic.
- Shared WSL/Windows time is unreliable. Preserve externally corroborated UTC and
  uncertainty; use monotonic RAW elapsed time and fail closed when trust expires.
  Never relax freshness thresholds or adjust the host globally to hide failures.
- Same-venue adapter aliases are one liquidity venue. Only verified canonical
  identities may compare; wrapped/network/unknown assets remain distinct.
- Buy asks and sell bids with depth, market limits, taker fees and exact Decimal
  arithmetic are mandatory. Never assume stablecoin parity, maker execution,
  missing fees as zero or future transfer sale prices as guaranteed.
- Actionability is independent of observed profitability and lifecycle status.
  All current routes remain false until separately verified eligibility, funding,
  fees and clock health; public API access never establishes Alberta eligibility.
- Preserve immutable evaluation/book/fee/FX/clock/config provenance and original
  availability timestamps. Historical replay must never use future information.
- The scanner only projects inventory and transfer scenarios. Stage 4's separate
  paper coordinator owns virtual fills and journals. Estimates stay distinct from P&L.
- Production credentials must be provisioned separately in Stage 7; local owner
  credentials never enter progress documents, source, reports or logs.

## Stage 4 accounting and infrastructure protection
- Read docs/ACCOUNTING.md before changing simulation, balances, cost basis or reports.
- Validated and research capital, sessions and results are separate; classification
  cannot change after funding. Initial crypto consumes its sourced CAD-equivalent
  starting capital. Never create free inventory or combine scenario equity.
- Reserve owned units under session locks; each exchange leg commits independently.
  Use idempotency keys and immutable journal/event/evidence records on restart.
- Use the first valid post-delay observation, captured source/ingestion UTC and
  uncertainty. No future book/FX/cost basis may affect an earlier decision. A missing
  observation creates a missed/failed result, never an invented fill.
- Moving weighted-average CAD basis, matched-leg cashflow profit and holding gains
  are distinct. Unknown values remain null. Reconcile ledger and portfolio; never
  subtract fees/expenses twice. Preserve the original-holdings benchmark.
- Paper worker runs inside the singleton collector. Keep pending queues, evidence
  caches, API queries and connection pools bounded; never prune financial history.
- Protect existing production services, data and dependencies; source changes do not authorize infrastructure maintenance.

## Stage 5 terminal, funding and export invariants
- Read docs/TERMINAL.md, FUNDING.md and REPORTING.md before changing terminal,
  allocation planning or exports. Charts may use floats only for display, with exact
  tooltips; browser numbers never determine trades, ledger or quantitative decisions.
- Funding plans are hypothetical initial contributions. Conserve exact total CAD
  capital, price crypto with sourced evidence, show both-leg deficits, and preserve
  independent experiment capital/benchmarks. Reset creates a new research session;
  idempotency prevents accidental duplicate funding. Coverage is not actionability.
- Preserve paper-depth-v1.1.0's conservative flooring of derived slippage-adjusted
  budgets before strict NUMERIC validation; optional owner slippage caps fail legs
  rather than rewriting prices or promising atomic execution.
- The analytical ZIP contract is quantix-export-v1. Preserve stable mandatory CSV
  headers and exact decimal strings, opening history and self-contained evidence.
  Strategy filters scope trade metrics; whole-session accounting remains explicit.
  No AI conclusions or secret/config environment dumps may enter an export.
- Bound export jobs, SQL batches, size/time and temporary artifacts. Large reports
  fail explicitly; never silently omit requested financial records. Stream downloads.
- Polling is authenticated, bounded, coalesced, paused in hidden tabs and reconnects
  with backoff. Current data, retained evidence and unavailable telemetry stay distinct.
- Saved terminal market preferences and the collector's applied version are separate;
  the owner version applies at the next bounded allocation tick. Notification and
  NVIDIA controls remain inactive through Stage5; do not begin Stage6 automatically.

## Complete retained-data exports
- Main Export Everything for ChatGPT uses quantix-full-v2 background jobs and all retained
  owner portfolios/history. Preserve the secondary quantix-export-v1 custom contract.
- Read docs/REPORTING.md and FULL_EXPORT_VERIFICATION.md before changing export scopes,
  privacy projections, snapshot/count verification, locks, disk limits or streaming.
- Every added database model needs an explicit export/exclusion policy. Never silently
  omit/truncate source rows, export private Telegram/auth records, or aggregate independent
  portfolio equity. Keep known-empty and unavailable/retention-expired data distinct.

## Size-bounded analytical exports
- Final downloadable ZIPs must be <=512,000,000 decimal bytes, verified after close
  with CRC/integrity checks before atomic publication. Never weaken the cap.
- Preserve all financial/decision rows, exact decimal strings and source references.
  First try lossless typed JSON DAG deduplication; ship the standalone loader and
  decoded JSON semantic hashes. Keep readable analysis/coverage/history projections.
- If measured ZIP/scratch exceeds budget, retry once within the same read-only snapshot
  as an explicitly labeled analysis snapshot. Disclose per-dataset raw-payload reductions
  and counts/methods; preserve critical/linked execution and profitable evidence.
  Never delete or aggregate the source PostgreSQL history to shrink an export.
- Scratch has a 2,000,000,000-byte budget and a 6-GiB free-disk reserve; jobs are singleton,
  cancellable and clean partial artifacts. No partial or old oversized download is served.
