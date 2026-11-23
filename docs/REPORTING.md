# Reporting contract v1.0

Stage 8A adds observational timing JSON within existing book payloads, evaluation
evidence, collector heartbeat and notification events. No table, CSV header or export
scope is added/removed. Both quantix-export-v1 and full-v2 retain exact financial
strings, privacy policy, complete retained-source evidence and the 512,000,000-byte
full ZIP publication cap. Timing definitions/provenance are documented in
`MARKET_DATA_TIMESTAMPS.md` and embedded in each ZIP README; durations are monotonic measurements and nullable
exchange times remain nullable. Live-only post-commit acknowledgments are not
retroactively inserted into historical DB payloads. No historical timing is invented.

Stage 8C native books carry `raw_evidence.stream_integrity` within the existing
book and evaluation payloads when retained. It records the native channel,
verification method, stream generation and linked sequence fields where
available. Receipt and exchange event timestamps retain their separate
meanings. The collector's live counters summarize its bounded runtime window.
[WebSocket architecture](WEBSOCKET_ARCHITECTURE.md)

The executable contract lives in `arbiq.analytics.contracts`; authenticated
`GET /api/v1/reports/contract` (browser BFF: `/api/reports/contract`) exposes JSON
Schemas, ordered CSV headers and the required ZIP manifest. Stage 1 provides the
interface and validation. Stage 4 supplies deterministic portfolio/performance/daily
JSON aggregation and per-trade evidence; Stage 5 implements the complete authenticated ZIP.

## Common format

- Reporting period is half-open `[start, end)` in ISO-8601 UTC; daily reporting uses
  America/Edmonton local midnight converted to UTC, including 23/25-hour DST days.
  generated_at is separate from the data cutoff. Automatic current-day snapshots
  are provisional revisions with explicit as_of, not an invented midnight close.
- All monetary/quantity/rate JSON values are base-10 strings. CSV uses plain decimal
  notation without thousands separators, UTF-8, RFC 4180 quoting and a header row.
- Currency is explicit. `_cad` is Canadian dollars; quantity/price fields include
  canonical asset/quote-asset identity. `_pct` is percent (0.5 means 0.5%, not 50%);
  fee rates are fractions (0.001 means 0.1%); delay is milliseconds.
- Null JSON / empty CSV fields mean unavailable, never zero. Zero means measured
  zero from complete records. No financial data is synthesized to fill gaps.
- IDs join records; ordered rows sort by relevant timestamp then UUID. `*_json`
  fields embed canonical JSON with decimal strings, source/version IDs and evidence.
- Exports identify selected paper session IDs and mode=paper. Actual trades cannot
  enter paper metrics. CSV text must be escaped against spreadsheet formula injection
  while retaining original text in JSON provenance; decimal numeric signs stay intact.
- config_snapshot.json contains public effective strategy versions, calculation
  versions, schema/period/unit metadata, exchange/fee assumptions and valuation/FX
  sources; never environment dumps, password hashes, chat IDs, tokens or session data.

## Daily summary definitions

| Metric | Definition for aggregation |
| --- | --- |
| Detected opportunities | Distinct opportunities with detected_at in period |
| Telegram alerts | Distinct signals with a successful Telegram delivery in period; retries are not extra alerts |
| Simulation attempts/outcomes | Cohort requested_at in period, classified at cutoff as pending, completed, partial, missed or failed; counts must reconcile |
| Gross simulated profit | Matched disposal proceeds less matched cost basis before fees/expenses; no double-counting cost basis fees |
| Trading fees / expenses | Separately converted CAD fees and transfer/rebalancing/execution expenses at their documented effective FX |
| Net simulated realized P&L | Gross simulated profit minus trading fees and expenses under a documented recognition policy |
| Unrealized P&L | Marked remaining virtual inventory less remaining cost basis; explicit valuation/liquidation assumptions |
| Portfolio equity CAD | Inventory + cash converted at as_of sources; no double-counted reserved balances or system equity/expense accounts |
| Capital utilization | Reserved/committed CAD capital divided by deployable CAD portfolio equity; 0–100%, unavailable for zero denominator |
| Maximum drawdown | Largest percentage fall from flow-adjusted equity high-water mark in the period; initial baseline and valuation frequency disclosed |
| Best/worst exchange pairs | Ranked matched realized simulated net P&L for buy→sell exchange pair, counts and fee allocation disclosed |
| Failure reasons | Stable reason-code counts for missed/failed simulations and rejected signals, scoped explicitly |
| Initial CAD comparison | Equity less initial CAD basis less subsequent net external funding; report starting FX separately |
| No-trading benchmark | Initial virtual asset quantities marked using the same current FX/price sources, with documented handling of later external flows |

If fills span periods, realized P&L is recognized at disposal/fill time while attempt
counts stay in their requested_at cohort. Report scope and cutoff explain differences.
Stage 4 uses per-exchange/asset moving weighted-average CAD basis, as explicitly
specified in [ACCOUNTING.md](ACCOUNTING.md). Disposal-basis P&L and matched two-leg
cashflow P&L are distinct. Fees already included in basis/cashflows must not be
subtracted twice; unknown CAD conversion remains null.
Do not claim full two-leg success when only one leg fills. All displayed P&L must say
simulated; no estimate is realized real-world profit. Deterministic analytics, not AI,
produces metrics. Missing/stale marks must produce quality notes and null affected totals.

## Export for ChatGPT ZIP

| File | Contents |
| --- | --- |
| SUMMARY.md | Human-readable scope, definitions, period, units, calculated metrics, caveats and manifest; all conclusions traceable to records |
| metrics.json | AnalyticsExportMetrics numerical envelope plus scope/period metadata; explicit nulls for unknowns |
| opportunities.csv | Estimates and reconstructable book/fee/FX/calculation provenance |
| signals.csv | Decisions, reasons and delivery-attempt history |
| paper_trades.csv | Attempts/outcomes, delays, reasons and versioned assumptions; nested order details in provenance |
| paper_fills.csv | Every fill with quantities/prices/fees/FX/cost basis and journal linkage |
| portfolio_equity.csv | Valuations, equity/P&L, utilization/drawdown, starting and no-trading bases |
| balances.csv | Per session/exchange/currency/account role balances; nested immutable journal/ledger details for reconstruction |
| fees.csv | Fees plus rebalancing/other expenses, units, FX and effective assumptions |
| system_health.csv | Connectivity/data-quality/operational evidence for the period |
| config_snapshot.json | Sanitized settings/versions and complete reproducibility metadata |

All 11 files are required, even when CSV files have only headers. No extra filename
is silently required. Referenced pre-period books, fee versions, FX, opening cost lots,
initial holdings and ledger records must be embedded in provenance/config so the ZIP
is self-contained. Stage 5 implements export reconciliation, stable serialization,
size limits, access control and artifact checksums. AI-written commentary is optional
and can never replace underlying records or change metrics.

## Stage 4 numerical evidence

Authenticated per-session `/paper/sessions/{id}/metrics`, `/portfolio` and `/daily`
return classification-scoped exact strings and limitations. Full-period SQL totals
are independent of bounded detail/group pages. Daily revisions and checkpoints are
append-only. `/paper/trades/{id}` uses `paper-trade-evidence-v1`, joining the pinned
observations, orders, fills, events, fee/FX versions, journals/accounts/entries and
initial benchmark. The dashboard downloads this JSON without AI-generated analysis.
The requested_at attempt cohort, completed_at trade cashflow and effective_at
accounting period are explicitly identified. Null win rate means no completed
CAD-valued sample. Research never enters validated primary metrics.

## Stage 5 implemented API and export envelope

`GET /api/v1/analytics/sessions/{session_id}` accepts explicit ISO UTC `start`, `end`
and optional `strategy_version_id`. Authentication and portfolio ownership are
mandatory. It returns period-scoped numerical trade/accounting results, the last
retained portfolio checkpoint strictly before the cutoff, opening checkpoint where
available, full-ledger reconciliation and limitations. Missing retained valuation
is null, never an invented current price. The default period is from the later of
session initialization or seven days before the cutoff; maximum period is 366 days.
Naive timestamps, reversed ranges and future cutoffs are rejected.

`GET /api/v1/analytics/sessions/{session_id}/series` returns at most 500 retained
checkpoints, evenly sampled by row index with first/final endpoints retained. It
never interpolates financial values. Full-period maximum drawdown still uses every
checkpoint, so downsampling cannot conceal the numerical maximum. The response
identifies available count, sampling and omissions. Exact values remain strings.

Historical outcome counts exclude completions/fills recorded after the requested
cutoff. They use immutable prior lifecycle evidence for pending states. Trade-size
performance groups by the pinned configured CAD trade budget, not a claim that
that entire budget was spent. Asset, route, delay and strategy groups retain exact
known-CAD completed simulation results and explicit bounded group windows.

A strategy filter selects trade metrics. Portfolio equity, benchmark and accounting
remain whole-session: shared inventory cannot be retrospectively allocated to a
strategy without inventing capital. ZIP reconstruction datasets preserve all
session dependencies, including other versions and pre-period opening history;
`paper_trades.csv` provenance includes `selected_strategy_member` and
`in_reporting_period`. This scope is stated in metrics, configuration and README.
Research and validated sessions are always independent and never aggregated together.

`GET /api/v1/reports/export?session_id=...&start=...&end=...` additionally accepts
`strategy_version_id` and streams an authenticated ZIP download. It is generated
from one repeatable-read, read-only PostgreSQL snapshot and then audited through
ReportExport/SystemEvent records. A private 0600 temporary file is deleted after
serving or on error. No public static artifact URL, environment dump, auth session,
password hash, API credential or chat ID is exported. Credential-bearing metadata
keys and URL query values are redacted. CSV text formula prefixes are escaped;
financial decimal signs and precision are preserved.

The actual export envelope is `quantix-export-v1`, described by executable
`AnalyticsExportMetrics` and `AnalyticsExportManifest`. The initial DailySummary/
ExportManifest classes remain available as legacy contracts; they are not silently
claimed as the richer ZIP envelope. Mandatory CSV headers and the eleven-file
minimum remain unchanged. `export_manifest.json` lists every data member, exact
uncompressed byte count, record count, ordered columns and SHA-256. Its own checksum
is excluded to avoid circular hashing. The HTTP response supplies the whole-ZIP
SHA-256 separately.

The ZIP includes mandatory files plus these reconstruction tables:

| File | Definition |
|---|---|
| journals.csv | All session journals effective before cutoff, including initial contributions and reservations |
| ledger_entries.csv | Exact asset-unit debits/credits; join journal_id/account_id and aggregate per journal and asset |
| accounts.csv | Referenced session/exchange/asset roles; available/reserved are inventory, equity/expense are counteraccounts |
| accounting_movements.csv | Moving basis, realized trading/FX, fees, expenses and external flows linked to journals |
| orders.csv | Original requested legs with fill-derived cutoff status; later mutable status is excluded |
| lifecycle.csv | Immutable simulation transitions available before cutoff |
| execution_observations.csv | First causal post-delay evidence and captured clock/FX provenance |
| order_books.csv | Complete referenced depth, original source and receipt timestamps, confidence/hash |
| fee_versions.csv | Referenced immutable maker/taker assumptions; marketable execution uses taker |
| fx_quotes.csv | Referenced explicit conversion/initial-funding rates with source/provenance |
| markets.csv / assets.csv / exchanges.csv | Joined registry identifiers; current registry metadata does not establish historical trading eligibility |
| strategy_history.csv | Pinned session strategy versions and their public settings |
| notification_attempts.csv | Existing recorded attempts; no delivery is invented, and later mutable outcomes are labeled unavailable at cutoff |
| opportunity_invalidations.csv | Retained market-opportunity lifecycle reasons in the selected period |
| benchmark.csv | Original-holdings benchmark alongside the same retained equity timestamps |
| research_scenarios.csv | Selected research scenario and initial capital/benchmark; header-only for validated sessions |
| README.md | Units, accounting/period rules, reconstruction joins and column dictionary |
| export_manifest.json | Complete member hashes, sizes, row counts, schema, scope and reconciliation |

`opportunities.csv` contains immutable evaluation rows (ID is evaluation UUID) with
route identity and exact historical inputs/results in provenance, plus legacy
Opportunity rows if recorded. `detected_at` is the route's first detection; evaluation
availability remains `provenance_json.evaluation.evaluated_at`. Counting evaluation
rows is not the count of distinct detected routes. Network detections are explicitly
network-wide public scanner activity rather than strategy-attributed profit.

Generation streams SQL records in batches of 200 into ZIP members; it never loads
all fills/books/ledger entries into memory or constructs a complete client Blob.
One export runs per API process. Each member is limited to 100,000 records, and total
uncompressed output to 256 MiB. Exceeding a limit returns HTTP 413 with a shorter-period
instruction; it does not silently truncate or mark an incomplete archive complete.
A concurrent request receives 429/Retry-After. Empty tables have authentic headers
and zero rows. The frontend uses the authenticated download route directly.

Financial correctness is inherited from immutable records. Ledger balancing and
nonnegative inventory are checked independently; retained equity reconciliation is
reported separately and remains null when a valid checkpoint is unavailable.
Completed known-CAD sample count accompanies win rate; zero samples yield null.
Neither a low-sample win rate nor an observed spread is a real-world profit promise.

### Generation responsiveness and wide fields

Large evidence can produce legitimate CSV fields larger than Python's default
128 KiB parser limit. The ZIP's README includes a streaming Python example using
`csv.field_size_limit(256 * 1024 * 1024)`, matching the bounded uncompressed archive
size. Do not truncate provenance to satisfy a reader's default field limit.

CSV cell normalization, JSON serialization, hashing and compression now run in one
sequential background thread job per row. SQLAlchemy attributes are extracted on
the event-loop thread, then nested evidence remains referenced until that row's
worker completes; no unbounded task queue or additional full-history copies exist.
Normalization walks each node once, with a bounded 2,048-entry credential-key cache.
ZIP DEFLATE level1 reduces CPU work without changing uncompressed member bytes,
financial precision, columns, order or provenance. JSON/text members are also written
outside the event loop. Cancellation waits for the in-flight write before closing
the member/file, avoiding an abandoned thread writing into a closed ZIP.

Generation has a 90-second deadline, with one in-flight row drained for safe cleanup;
expiration returns413 with a shorter-period instruction rather than an incomplete
archive. Other authenticated API requests can continue while compression runs.
Synthetic wide-member and cancellation regressions verify responsiveness and CRC
integrity. Actual before/after workload measurements belong in PERFORMANCE.md.

`terminal_settings_history.csv` retains only the owner's versions effective before
the cutoff. `config_snapshot.json.terminal_preferences_at_cutoff` selects the last
such version, or null plus an explicit limitation. It never substitutes today's
post-cutoff preferences for historical configuration. These settings contain public
coverage/notification preferences only, and `notification_delivery_active=false`
remains explicit throughout Stage5. Current route.run_id is mutable, so unproven
historical collector-run configuration is not presented as pinned calculation input.


## Stage6 notification evidence extension

The stable quantix-export-v1 envelope and all11mandatory files remain. Current ZIPs
have35members. signals.csv additionally contains owned AlertDecision rows joined to
retained evaluation/route IDs with explicit ACTIONABLE/RESEARCH_ONLY classification
and eligibility. alert_decisions.csv, alert_outbox.csv and alert_events.csv preserve
source/rule/settings decisions and immutable delivery audit. These are owner-wide
network alerts, never attributed to a selected paper portfolio or strategy P&L.
Private pairing/bindings/destinations, provider message IDs and command/message text
are excluded. No bot token is stored in these tables or exported.

Mutable outbox outcomes after cutoff cannot establish an earlier status; unproven
historical status, attempts, retry availability and errors stay unknown. Only a
recorded delivery timestamp before cutoff proves delivery. Config activity is false
when explicitly disabled at cutoff; otherwise null rather than inferred from today's
pairing/runtime. notification_counts exposes owner-scoped decision/eligible/research,
actual alert delivery and expiry-before-delivery counts, with period/scope labels.


## One-click full retained-data export (quantix-full-v1)

The main **Export Everything for ChatGPT** control requires no portfolio, date,
strategy, asset or exchange selection. It calls authenticated `POST /api/v1/reports/exports`
with `{}` and receives202/job ID immediately. Authenticated status endpoints are
`GET /api/v1/reports/exports/current` and `GET /api/v1/reports/exports/{id}`;
`GET /api/v1/reports/exports/{id}/download` serves only a completed ZIP. The browser
BFF mirrors these under `/api/reports/exports`. Jobs and downloads remain owner-scoped;
a job ID is not authorization. Completed files expire after24 hours.

The separate **Advanced custom-filter export** uses the same job/status interface
with `scope=CUSTOM`, session and optional applied period/strategy. Its stable
quantix-export-v1 members and existing explicit limits remain. Errors now appear
in the UI rather than being saved as misleading export.json downloads. Legacy
GET `/reports/export` remains API compatible; the UI no longer downloads its errors.

Full export uses one independent connection, repeatable-read/read-only PostgreSQL
snapshot and server cursors of8 rows, sequential disk-backed ZIP64 writes/compression
outside the event loop, and no date or record-count cap. Every dataset has an independent
COUNT in the same snapshot, matched to its actual streamed count before publication.
The full-export lock and legacy semaphore prevent overlapping generators. An
interrupted job is marked failed on shutdown or subsequent status/recovery; partial
files are deleted. A page refresh resumes status and exposes the completed download.
No broker, extra production service or new schema migration is required.

Safety limits:2 hours,24 GiB uncompressed output, at least6 GiB free disk (above the
existing5 GiB service guard). Limits abort with explicit failure and no partial ZIP;
they never label omitted/truncated data complete. The existing API/DB container caps
remain. Job metadata is0600 and the export directory0700 on the private export volume.
Generation is independent of the HTTP request; status calls are short. The BFF
streams the final ZIP without a browser Blob and without a10/120 second body deadline.
The existing proxy's150 second *idle* timeout does not limit an actively streaming download.

Root members: START_HERE.md, SUMMARY.md, manifest.json, data_dictionary.md, README.md,
checksums.sha256. Folder CSVs contain every retained source-model record within
owner scope plus shared public scanner/market/health evidence. analytics/metrics.json
contains separate per-portfolio performance, checkpoint, benchmark and exact ledger
reconciliation; independent portfolio equities are never added. All raw histories
remain available even where existing summary group windows are bounded.
Manifest files list byte lengths, SHA256, columns, actual and independently counted
source rows, timestamp ranges, source table/scope, excluded columns and empty states.
It lists all preceding members; checksums.sha256 also covers manifest.json and every
other member except itself, avoiding circular hashes.

Excluded sources are explicit: users/auth_sessions/login_attempts and private Telegram
challenges, bindings, worker update offsets. Message bodies, provider destination/message
IDs, actor/user identifiers and credential-bearing nested metadata are excluded/redacted.
Immutable financial IDs remain for reconstruction; actual owner-recorded trades remain
separate from paper records. No environment or filesystem log dump is included.

The archive includes all retained portfolios (including independently funded research),
strategy/config versions, evaluations/rejections/budget diagnostics, market/asset universe,
latest and historical retained books/observations, fees/FX, paper legs/fills/transfers,
ledger/inventory/checkpoints/daily summaries, alert decisions/outbox audit and retained
connectivity/resource telemetry. Unsaved in-memory data, deleted samples, private provider
messages and unavailable host telemetry cannot be reconstructed. Expired counts remain
null when not known. Capture timestamp and source timestamps stay distinct; this is a
snapshot of retained history, not a guarantee of complete historical market coverage.

