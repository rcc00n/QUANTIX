# Stage 8B capital allocation design

Stage 8B creates independently funded paper research experiments. Opening
contributions transfer exact virtual quantity from the experiment's equity
accounts. They establish contributed capital and the original-holdings
benchmark. They produce no P&L and cannot modify another session.

The combined Stage 8C candidate carries the four policies and Stage 8B.1
export and Radar changes. Native depth uses the same clock, fees, FX,
inventory limits and causal decision evidence as REST. Order-book
synchronization gives transport evidence. Funding and actionability still
require their independent checks. [Depth architecture](WEBSOCKET_ARCHITECTURE.md)

## Controls and policies

The Funding Planner submits total CAD capital, minimum Buy Capital, target
purchase size, minimum useful position in CAD, minimum distinct funded venues,
reserve target and cap, venue/asset/stablecoin caps and allowed exchanges,
base assets and inventory currencies. Stablecoins are separate canonical
assets. Eligibility scope is explicit. The validated request fails closed
because the application cannot independently verify account eligibility and
fees at either venue. This interface funds only research. Telegram retains its
separate C$50 minimum default.

| Policy | Allocation rule |
|---|---|
| Legacy / COVERAGE | Reproduce old maximum-net size under the target cap and maximum per-cell sharing across alternative routes. The causal comparison is study-only and uses the same strict tier scorer as modern policies. The preexisting proposal endpoint remains available for older integrations. |
| Liquidity Weighted | Prioritize larger executable depth and better observations. Apply the constraints to complete buy/sell bundles, with smaller meaningful tiers when required. This can deliberately measure inventory capacity on currently loss-making routes. |
| Route Optimized | Prioritize after-cost positive native-book sizing with available CAD marks, reliability and historical frequency. Return an infeasible plan when the retained evidence supports zero such routes. |
| Diversified / Hybrid | Balance opportunity evidence and executable depth with diversification and concentration penalties. Apply the same hard caps and reserve rules. Nonpositive routes can contribute to a capacity study, with their result shown. |

Each strategy receives the same initial contribution in comparison. The
planner allocates both requirements together using native assets, conservative
taker fees, market order and notional minimums, exchange quantity/cost
precision, buy slippage and sell base fees when applicable. CAD contribution
cost comes from pinned recent executable depth and fee-adjusted FX. Caps
include reserve and the largest venue share groups same-venue aliases.
An infeasible result shows exact unplaced CAD and a reason. The sum of placed
and unplaced value equals input capital exactly. The system cannot select an
infeasible portfolio for funding.

Fixed size points are C$50, 100, 250, 500, 1,000, 2,500 and 5,000, plus the
minimum and target requested. A tier uses actual rounded fee-inclusive
Buy Capital and can be absent because the book has insufficient depth or no
practical lot size. Balance checks use spendable native quantities. Available,
reserved, required and deficit quantities remain distinguishable. The portfolio
coverage view reports the largest supported capped size evaluated, exact CAD net
for that size, limiting balances and a reallocation assessment. Its capped
lattice and tier search is bounded and can conservatively miss a feasible
intermediate size. Increased funding cannot establish nonnegative net profit,
freshness or validated execution.

The route view carries exact modeled trading fees, slippage reserve,
rebalancing cost and FX drag from the pinned or funded evaluation. The FX
drag is already reflected in conversion cashflows and must never be
subtracted again. Unavailable costs remain null.

An alternative is a route that could independently reserve both cells at a
tier. Simultaneous coverage is a deterministic pass that subtracts each
allocated buy quote and sell base before considering the next route. It is
constructive coverage under that ordering. The optimizer makes no global
optimality claim. Idle reserve is excluded from planned trade coverage. The
current balance view uses actual available ledger balances.

## Evidence, immutability and time

The authenticated comparison uses one fixed UTC cutoff and the same bounded
window and shortlist for all four modes. The shortlist orders retained
quality-compatible route frequency before admitting lower-quality routes.
Ranked evidence includes evaluation and book identifiers, original receive and
source times, depth, FX paths and fee evidence. Both recorded availability
and observation times must precede the decision. Local durations use monotonic
time. Trusted clock, plausible/stable book timestamps, coherent cross-book
receipt, admissible book quality and causal conversion evidence are checked
before sizing. Evaluation counts outside the scan window or expired history
remain unavailable. No look-up substitutes a modern mark into a historical
comparison.

Historical proposals are read-only. Current portfolios require a feasible
selected proposal, trusted UTC and fresh marks at selection and commit.
An owner row lock serializes creation, and a proposal-scoped idempotency key
returns the same session receipt on replay. A changed request for that
proposal conflicts. Session configuration, market scope and chosen allocation
are pinned. Existing sessions and previously pinned execution remain
independent. There is no schema migration. The existing proposal and experiment
rows and initial ledger evidence carry the allocation version and source
references. They are included in the retained full ZIP. The v1 custom
export schema and 512,000,000-byte full ZIP cap remain in force.

Pricing is on demand, uses at most 30 routes and at most nine distinct size
points, and runs in a single serialized planning slot per backend process
with the existing request rate limit. CPU-bound preparation uses a worker
thread. No allocator runs in the collector. Selection uses the existing
append-only ledger and session locks. Market cohorts are deliberately bounded.
The result shows the limit, excluded evidence and infeasible size targets.

## Execution, valuation and transfer previews

New simulations record `paper-depth-v1.2.0`. Paper legs reserve both requirements
before scheduling and settle independently using the first valid observation
after their execution delay. Adverse principal and fees are rounded exactly
before a virtual debit. A partial or one-sided outcome is reconciled as native
inventory exposure and does not become matched-cycle profit. Previously recorded
strategies, calculations and fills retain their versions and values. Current risk valuations batch
source resolution and the FX graph while keeping the same Decimal rules.
Marks and converted account exposure remain nullable. Fresh, partial, stale
and unknown valuation show exact affected assets and venues. The original
holdings benchmark remains a separate valuation.

The read-only rebalance preview takes the owner's scenario assumptions for a
same-asset network, withdrawal/deposit availability, fees and settlement
delay. It compares remaining destination deficit and native fees with doing
nothing. It accounts for existing reservations and a requested source floor.
Unavailable networks and unknown timing or fees block estimates. A conversion
would require a separate executable FX path and is shown as blocked. Unknown
CAD fee and future P&L stay null. No transfer, conversion, deposit, withdrawal,
balance update or external order is performed.
