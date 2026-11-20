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

