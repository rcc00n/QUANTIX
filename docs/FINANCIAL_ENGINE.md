# QUANTIX depth engine

Current local calculation version: `quantix-depth-v1.1.0`. All results are **projections**, never
orders, fills, realized profits or evidence of trading eligibility. The pure engine
has no exchange client, wall-clock read, AI dependency or ledger writes.

## Inputs and exact arithmetic

`opportunities/contracts.py` defines frozen inputs. Observed price/quantity levels
use the existing strict decimal contracts. Binary floating point, nonfinite values,
nonpositive depth, crossed books and inconsistent order limits are rejected.
All arithmetic runs in an isolated Decimal context with 78 significant digits, so
the caller's decimal context cannot alter an outcome. Derived FX reciprocals keep
their precision in evidence JSON; they are not rounded into invented exchange quotes.

`MarketInput` contains the canonical base identity, distinct quote currency, liquidity
venue group, actual asks/bids, taker fee evidence, exchange precision/minimum/maximum
limits and optional CAD conversion evidence. Identical venue groups and mismatched
base identities cannot form a route. The collector/risk layer additionally checks
identity provenance, active status, connectivity, timestamps, host clock and eligibility.

Every fee requires an explicit rate. The live scanner uses the maximum of its
conservative floor, usable published entry-tier taker rate and normalized adapter
taker metadata. The version records market scope, fee asset, rate and evidence
age. Immediate execution uses taker fees. Public schedules and stale schedules
remain estimates for the account. See [Stage 9 fee evidence](STAGE_9_REPORT.md).

The selected book carries the native trading constraints available for that spot
product. An explicit market-order prohibition blocks a market execution. Unknown
precision, limits and external-denomination restrictions remain visible in the
calculation evidence.

