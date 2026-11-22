# Opportunity research API

All endpoints below require the existing owner session. The Next.js `/api` proxy
forwards only explicitly allowed routes and requires same-origin POST requests.
Amounts, rates, prices and quantities are decimal strings; unavailable calculations
are JSON null. Nothing in these APIs submits orders or records simulated fills.

| Method | Backend path under `/api/v1` | Behavior |
| --- | --- | --- |
| GET | `/opportunities/budgets` | Supported CAD budgets, default and custom-budget bounds |
| GET | `/opportunities/diagnostics` | Live engine, coverage, processing and UTC diagnostics; explicitly unavailable fallback |
| GET | `/opportunities` | Filtered, paginated current projections or retained history |
| GET | `/opportunities/{uuid}` | Calculation inputs, cost breakdown, lifecycle and evidence references |
| POST | `/opportunities/{uuid}/recheck` | Bounded collector recheck request; 202 means queued, never confirmed |

List parameters: `budget_cad` (positive, at most 1,000,000, up to two decimal places),
`limit` (1–100), `offset` (0–500), `sort` (`net_profit` or `net_return`), `exchange`,
`asset`, `status` and `history` (false by default). Both directional venue names
match an exchange filter. Asset filters match normalized display symbols. The
response contains `items`, `total`, `limit` and `offset`.

Custom-budget sorting evaluates at most the most recent 500 matching routes before
pagination. `window_limited`, `matching_retained_routes` and `candidate_window`
make this bound explicit. Narrow exchange/asset filters to inspect older records;
individual retained IDs remain directly accessible. This prevents an unbounded
historical request from running thousands of financial optimizations on a small VPS.

Current in-memory engine projections can update more often than durable evaluations.
The API merges only matching persisted route IDs. A detail response identifies the
last durable evaluation separately as `retained_evaluation_id`; a newer live view
has `evaluation_id=null`. Its evidence nests older snapshot/fee references under
`retained_snapshot_references` with explicit attribution. They are never substituted
for the current calculation inputs. The immutable durable history is unchanged.

Freshness uses the oldest supporting book receipt, not the time a calculation was
repeated. Trusted externally anchored UTC permits expiry checks. Missing live
clock confidence, unhealthy host time or unavailable scanner liveness prevents a
verified current display. Stale evidence is displayed as EXPIRED and omitted from
the default current list. Persisted diagnostics cannot establish fresh clock trust.

Changing a budget reuses pinned inputs at their original evaluation time. It is a
historical counterfactual, not a fresh quote. Missing inputs suppress quantity and
CAD profit rather than inventing a conversion or cost. The API never derives
actionability from a positive projected return: all Stage 3 routes remain
`actionable=false`, with Canadian/provincial/account and funding eligibility
unverified. Recheck requests create an owner-attributed audit event; queue acceptance
does not guarantee that a discrepancy will persist.

Every non-CAD leg retains its conversion deadline. If trusted current UTC passes
that deadline, current outer CAD profit, return and acquisition-cost fields become
null with `CAD_CONVERSION_EXPIRED`. Missing/unverifiable conversion freshness also
suppresses those fields. Source timestamps are rechecked independently of fresh
ingestion times. Detail cost breakdowns preserve the original historical projection,
with `evaluated_at_utc`, explicit historical attribution and current valuation
suppression reasons. Retained cost figures never substitute for current FX evidence.
