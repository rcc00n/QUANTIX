# Market-data timestamps — Stage 8A

UTC identifies observations; `CLOCK_MONOTONIC_RAW` (monotonic fallback) measures
elapsed work. Neither Windows/WSL nor browser wall time establishes freshness.
`TrustedClock` quorum, expiry, uncertainty and independent host-health gates remain.
No host clock or freshness limits were relaxed. All trading stays PAPER ONLY.

Timing fields denoted `pipeline.*` below are nested in `verification.pipeline`,
within the existing extensible evidence envelope. Strict older CapturedBook readers
remain compatible during rollback; no new top-level book field is persisted.

| Field | Meaning / availability |
|---|---|
| `exchange_timestamp` | Nullable exchange event/snapshot/level-update time. Never manufactured. `raw_evidence.timestamp_basis` identifies the source. Legacy `normalized.observed_at` falls back to receipt and must not be mistaken for exchange time. |
| `request_started_at` | Collector attempt before connector queue/pacing. Not network request start. |
| `pipeline.http_request_started_at` | aiohttp request start, including DNS/connect/request/body transport. Available for instrumented REST calls. |
| `pipeline.response_completed_at` | Complete response text available to the CCXT response hook, before JSON/adapter parsing completes. |
| `pipeline.adapter_parsed_at` | Native adapter returned its parsed book. |
| `received_at`, `pipeline.collector_received_at` | REST adapter return, or WebSocket frame receipt before decoding/application. Original availability is immutable. |
| `pipeline.collector_processed_at` | Normalization/validation completed. |
| `pipeline.cache_updated_at` | Accepted normalized observation inserted in bounded current cache. |
| `pipeline.persisted_at` | Successful DB commit acknowledgment for this observation, present only in live cache after an actual write. Throttled writes do not claim persistence. The DB payload cannot contain its own future commit acknowledgment; it remains null/absent there. |
| `evidence.timing.evaluation_started_at`, `evaluated_at` | Immutable UTC calculation start/end; individual route duration is separately measured by monotonic RAW. Legacy evaluation record time remains the decision time. |
| `AlertDecision.evaluated_at` | Fresh eligibility check after persistence lookup; source evaluation availability and original batch reference are separately pinned. |
| outbox `created_at`, queued event `queue_insert_observed_at` | Fresh trusted worker UTC at enqueue statement, before flush/commit. API/legacy rows without a worker reference retain logical caller time and disclose unavailable insert observation. `queue_requested_at` separately preserves the batch reference. |
| outbox `delivered_at`, event `provider_acknowledged_at` | Telegram accepted sendMessage, not device receipt or owner acknowledgment. Ambiguous sends remain ambiguous. |
| `status_generated_at` | Collector status serialization time, separate from every book time. |
| `last_status_check_at` | Successful read of live collector status; fallback uses persisted health projection time. Never an invented exchange heartbeat. |

`request_ms` is the complete CCXT call after local pacing/global queue, including
its weighted throttler and parsing. `http_body_ms` is aiohttp request start to full
text receipt, not pure wire RTT. `adapter_parse_ms`, `validation_ms`,
`pacing_wait_ms`, `connector_queue_ms`, `global_queue_ms`, `scheduling_lag_ms`,
`persistence_commit_ms`, `route_evaluation_ms`, `comparison_queue_ms`,
`source_to_evaluation_ms`, `receive_to_evaluation_ms` have separate definitions.
Source/receive-to-evaluation is the age of the older supporting observation at
calculation completion; source age is unavailable without both source timestamps.
Opportunity publication duration includes durable save when one occurs; suppressed
writes never manufacture publication samples. Scanner cycle duration is not a
route evaluation duration. API timing ends at response creation, not browser paint.

Stage 8C adds source-to-receive histograms for changed published stream books
with valid source times. `stream_parse_ms` measures protocol processing,
and `stream_queue_ms` measures elapsed time from frame receipt to the start
of publication. This second metric includes protocol work and coalescing
delay. `source_to_evaluation_*` also separates REST-only, mixed and
WebSocket-only pairs, and measures each stream leg independently where its
source time is available. Pair and leg samples describe different populations.
A Coinbase snapshot has no supplied event time. Heartbeats, unchanged depth
and pending updates replaced before publication do not add book samples.
[Transport audit](STAGE_8C_REPORT.md)

Every rolling stage distribution is capped at 600 observations and exposes sample
count, retained elapsed window, unit and maximum. p95 requires 20 samples; p99
requires 100. Current book distributions are cross-sectional, not rolling samples.
Percentiles use nearest rank. Correlated samples are not independent confidence
intervals. Missing stages, empty sample sets and absent source timestamps stay null.

Freshness is separate from structural confidence and feed transport:

- FRESH: conservative age upper bound <= configured `freshness_fresh_seconds` (2s).
- AGING: above FRESH, <= `min(freshness_stale_seconds, maximum_age_seconds)` (30s).
- STALE: above that bound. Existing engine limits can be stricter.
- UNAVAILABLE: no usable two-sided book.
- CLOCK_UNTRUSTED: reference/uncertainty unavailable, or any future observation.

The conservative bound includes current and captured clock uncertainty. Missing
capture uncertainty contributes 2s, never zero. Receipt-only freshness does not
verify source age. Source timestamps must also pass validation; no negative age is
clamped to zero. Structurally suspicious books remain quarantined even with a young
receipt. These classes do not override accounting, notification or actionability gates.

Kraken tracks heartbeat/message UTC and synchronized snapshot/checksum state
separately from quote timestamps. A quiet synchronized connection can be healthy
while its unchanged quote is old. Heartbeats never rewrite quote age or fabricate
fresh evidence. Reconnection starts with empty stream state and requires new
snapshots/checksums. Existing REST fallback remains rate limited. No new exchange
WebSocket transport is introduced.

Cache rejects older receipt/source times independently of ingestion counters, and
exact duplicate stream observations cannot refresh receipt. Identical REST content
can be independently reobserved; its unchanged source timestamp continues aging.
Rejections retain bounded evidence in SystemEvent plus counters in collector
heartbeat. No rejected observation is republished to scanner/paper as a new arrival.
On restart, bounded trusted persisted watermarks protect ordering without restoring
old books as live data. SQL fallback ordering also checks receipt/source ordering
across runs. Historical immutable financial evidence is never rewritten or pruned.
An earlier untrusted/future timestamp cannot poison this ordering watermark: a
subsequent trusted observation can replace it even when its valid UTC is earlier.
The ingestion counter still cannot regress. Both in-memory and PostgreSQL recovery
paths are regression-tested; an older update cannot displace a trusted prior book.

Each evaluated candidate pins buy/sell receipt and source age, source and receipt
skew, capture uncertainty, timestamp basis, FX evidence age, evaluation duration and
clock confidence. Missing source synchronization and uncertainty-expanded skew have
explicit downgrade reasons. Source timestamps are exchange-reported, not independent
proof of those venues' clock accuracy. All current routes remain non-actionable;
eligibility, fees, funding and independent confirmation remain separate requirements.

Browser ages advance from the server-reported age using `performance.now()`, without
`Date.now()` subtraction. The displayed counter excludes unmeasured network transit,
which its tooltip states. Live status and retained fallback are labeled. Polling
uses the existing authenticated, singleflight, deadline/backoff/hidden-tab lifecycle.
The 15s status / 5s selected-book cadence is disclosed; no browser number drives a
trade, ledger, classification or quantitative signal.


Notification timing never infers queue waiting from a historical PostgreSQL
transaction timestamp. Reports record generation_ms; enqueue records a fresh
worker-clock statement observation when available (commit time remains separate).
Provider request duration is captured immediately after HTTP returns, before later
DB work. Queue-to-provider acknowledgment samples require the new explicit enqueue
observation; legacy latency_ms is not silently treated as that metric. Clock loss
or a malformed/future historical enqueue observation yields null duration.
