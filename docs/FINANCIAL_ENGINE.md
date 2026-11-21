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

## Pre-funded depth calculation

`evaluate_route(buy, sell, requested_buy_quantity, now=..., costs=...)` consumes
buy-side asks and sell-side bids from best to worst. It fails if observed depth is
insufficient, without extrapolating a fill. Quantities are rounded down independently
using each exchange's step size. Both legs must satisfy their minimum/maximum amounts
and notionals.

For base-denominated buy fee `fb`, acquired base is `buy_quantity × (1 − fb)`.
For base-denominated sell fee `fs`, seller inventory required is
`sell_quantity × (1 + fs)`. Selling quantity is rounded down after accounting for
both base fees. Any residual acquired base is explicitly retained as inventory;
it is assigned no fabricated cash proceeds.

Quote-denominated buy fees increase purchase currency required; quote-denominated
sell fees reduce sale proceeds. Base fees already affect quantities. Their CAD
disclosure value uses the corresponding executed-side VWAP and is never deducted
twice. `fees_cad` includes these disclosed base-fee values. The defined gross cash
projection adds disclosed trading fees back to the cashflow before reserves, making
`gross_profit_cad − fees_cad − expenses_cad = net_profit_cad`.

`net_profit_cad` equals fee-inclusive sale cash proceeds in CAD minus fee-inclusive
acquisition cost in CAD minus configured slippage/rebalancing reserves. Return is
this net amount divided by total CAD acquisition cost, expressed in percent.
Reserves are configurable estimates; they do not assert liquidity beyond observed
levels or claim transfers have occurred. Unverified reserves are clearly classified.

Capital requirements list purchase currency on the buying venue **and** base inventory
on the selling venue. The buy budget limits fee-inclusive acquisition cost; it does
not pretend the pre-funded seller inventory is free. Projected inventory deltas are
separate from balances and accounting. Stage 4 implements actual virtual balances
and simulated execution.

