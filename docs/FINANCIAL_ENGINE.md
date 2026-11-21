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

## CAD and executable FX

`ConversionLeg` represents source→target executable depth. Rates are target units
per source unit, and quantities are source units. Selling A from an A/B book consumes
bids. Buying A with B consumes asks using reciprocal rates and converted source depth.
Each leg includes its fee once. Source-denominated commission is included in the
source depth and effective price. Target-denominated commission is deducted from
output. Routes contain at most three hops and have no cycles.

`convert(route, source_amount)` gives executable liquidation proceeds.
`required_input(route, target_amount)` works backward through fee-adjusted levels
to calculate exact acquisition cost. A quote's `FXConversion` holds independent
currency→CAD and CAD→currency sides from the same causal set of observed books.
The selector prefers paths that support the configured CAD probe, then fewer hops
and lower round-trip drag. Its deterministic breadth-first search is bounded to
128 edge examinations per currency and three parallel books per pair. CAD uses an
explicit identity. Stablecoin deviations, spreads and FX depth change projected
profitability. The oldest participating capture governs FX expiry. Both receipt
and source spans must fit the skew budget including recorded clock uncertainty.

Missing, stale, future or insufficient FX evidence suppresses CAD profitability;
the same-quote native result can remain visible with its limitations. Unverified
fee/funding evidence on an FX route prevents verified profitability. Acquisition and
liquidation cashflows already include FX spreads/fees. `fx_cost_cad` is an optional
half-round-trip drag diagnostic using executable opposite-side routes, explicitly
**not** an additional cost subtraction. It is null if that diagnostic lacks depth.
All route rates, fees, source IDs, availability times and snapshots must be retained
with the calling scanner's immutable evaluation evidence.

## Sizing and bounded optimization

`maximum_quantity` intersects buy asks, sell bids, both markets' maximum limits,
base-fee quantities and step sizes. `optimize_route` evaluates the default CAD budgets
50/100/250/500/1000/2000/2500/5000/10000 and custom budgets. Continuous scans can pass
`budgets=()` and calculate other budgets from pinned evidence on demand.

The optimizer evaluates book-depth breakpoints, market-limit boundaries, the budget
boundary and nearby rounded buy/sell steps. For at most 32 buy ticks it enumerates
the complete feasible lattice. Larger lattices use deterministic branch-and-bound:
the unrounded depth/FX cashflow function is concave, and endpoint tangent envelopes
upper-bound all rounded outcomes in an interval. It prunes an interval only when it
cannot improve the best feasible profit. Search is bounded to 4096 additional
evaluations per budget. If global optimality cannot be certified within that limit,
the result explicitly says `OPTIMIZER_GLOBAL_OPTIMUM_UNCONFIRMED`, is not verified,
and reports the best evaluated feasible size. The engine never silently calls an
uncertified size the global optimum. Equal-profit initial candidates prefer lower
acquisition capital.
The `max_search_evaluations` keyword accepts an integer from 1 through 4096; the
default remains 4096 for deterministic replay and detailed on-demand analysis.
Continuous scanners can select a smaller proof budget to protect collection latency.
A smaller budget does not relax financial validation: an unfinished proof retains
the feasible result with the explicit unconfirmed-optimum classification above.
Retained replay honors the search cap pinned with the original evaluation, reproducing
its bounded result. On-demand budget resizing uses the full 4096-evaluation bound
and identifies that different analysis budget explicitly in its optimizer metadata.

The retained-size comparison evaluates trade sizes at C$50, C$100, C$250, C$500,
C$1,000, C$2,500 and C$5,000 alongside the best size found within the chosen budget.
It reports which side's observed depth, native limits or conversion evidence limits
the size. It also reports the projected costs and both reserves using ledger rounding.
The comparison API caps search at the stricter pinned and configured evaluation
limits, and releases the database connection before CPU work. The response states
whether the optimum was certified. FX drag is a round-trip diagnostic. The net CAD
calculation already accounts for directional exchange rates in its cash flows.

For a non-increasing continuous profit envelope, the engine starts from the exact
minimum feasible quantity and a rationally aligned inventory size. Concave bounds
then prove whether larger sizes can improve the result, without evaluating every
depth level or thousands of tiny orders. It still considers rounding: the smallest
feasible order can lose more than a slightly larger order with less residual inventory.
Synthetic performance tests bound evaluation counts for fee-negative routes with
25-level books, and compare incompatible-step results against an exhaustive oracle.

Loss-making sizes remain available for diagnostics. A positive top-of-book spread
does not guarantee positive net profit, a minimum order, or meaningful capacity.

## Transfer scenarios

`transfer_scenario` is separate from pre-funded evaluation. It subtracts explicit
withdrawal and network base charges, checks remaining visible sell capacity/limits,
and shows today's hypothetical proceeds separately from a user-configured future
price haircut scenario. Network identity, deposit/withdrawal availability,
confirmations, delay and their evidence remain explicit. Today's sell quote is
never guaranteed after arrival. `guaranteed_executable` and `actual_execution`
are always false. Unknown network/funding/timing costs remain research blockers.

