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

