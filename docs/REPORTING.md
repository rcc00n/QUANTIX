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

