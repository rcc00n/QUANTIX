# Virtual Funding Planner and persistent terminal preferences

The planner creates independent **RESEARCH** experiments. It never deposits real money,
modifies the validated portfolio, replenishes a running portfolio or rewrites historical
balances. A reset creates a new session with equal initial capital; the original
experiment and its benchmark/history remain inspectable.

## Stage 8B comparison and selection

Compare Legacy, Liquidity Weighted, Route Optimized and Diversified / Hybrid
with one contributed capital amount, cutoff and route universe. The target
uses actual CAD acquisition after fees and both legs' native requirements.
Counts at each size distinguish routes that can be funded as alternatives from
routes that can reserve concurrently. The comparisons show reserve,
concentration, losses after costs and ineligible or stale evidence.

A historical cutoff produces a study. Only a feasible live plan with trusted
fresh opening marks can fund a new session. Select the proposal explicitly
before creating it. Repeating a creation request for the same proposal returns
its existing session. See the [Stage 8B design](CAPITAL_ALLOCATION_DESIGN.md)
and [fixed-cohort results](PAPER_FUNDING_COMPARISON.md).

## Inventory explanations

Authenticated `GET /api/v1/paper/funding/coverage?session_id=...` returns current
ledger-derived available and reserved quantities, and a bounded current-route window.
Pinned route inputs are resized using the selected session's actual trade cap,
conservative taker/slippage/reserve assumptions and depth constraints. The required buy
quote currency and sell base inventory are compared in their original units. Each
missing quantity is explicit; CAD, USD, USDC and USDT remain distinct. A transfer
scenario does not pretend it requires pre-funded destination inventory, but network,
costs and future-price uncertainty remain independent gates.

The result is a funding explanation for retained projections, not a fresh executable
quote. `inventory_sufficient` alone never grants verified eligibility or actionability.
Filters/limits are disclosed and the endpoint cannot silently claim complete coverage of
all discovered listings. The default route window is20, maximum50; expired or
future-updated routes are excluded using trusted application UTC.

The coverage route lists each leg's fee asset, required reserve, held units,
reserved units, spendable units and exact deficit. The requirements describe the
net-profit-optimized pinned candidate. A separate capacity estimate under the same
session trade cap shows the depth/FX-supported acquisition before applying balances,
the maximum affordable acquisition after applying both spendable balances, and the
amount that meets the session's minimum buy capital. This minimum is independent of
Telegram policy.

Two-sided CAD inventory marks value the required and funded reserves with pinned
liquidation depth before exit costs. The marked shortfall values the missing units
and can differ from the cash needed to acquire them. Return on funded inventory
divides the projected net CAD result by that position mark. Incomplete depth or FX
evidence leaves these fields unavailable. Both book source timestamps and receipt
timestamps are checked for skew including capture-clock uncertainty.

