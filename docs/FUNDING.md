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

## Automatic allocation

`POST /api/v1/paper/funding/proposal` accepts exact decimal `capital_cad`,
`trade_amount_cad` (default C$250), exchange and canonical asset filters, lookback 1–30
days, `COVERAGE` or `EQUAL_VENUES`, and a 0–100% cash reserve. Historical positive
**recorded evaluations** rank observed demand; this is not a forecast or profitability
guarantee. Owner venue/asset filters apply before ranking. At most 30 historical routes
and 40 venue/asset cells are considered, with 128 optimizer evaluations per route.
Proposals are bounded to 30 per hour and perform no additional public API polling.

COVERAGE resizes each latest retained compatible route at the target trade budget,
including conservative taker fees and slippage. It greedily admits the complete
buy-quote/sell-base pair only when fresh full-size marks for both initial inventory
contributions fit the remaining capital. A missing mark or insufficient capital rejects
the whole pair. Shared cells retain the largest required unit quantity for alternative
routes; they do not pretend every route can reserve it simultaneously. This avoids the
naive failure where 40 equal cells receive C$225 each and none can support a C$250
attempt. Cash remains cash when no complete route can be covered.

EQUAL_VENUES normalizes each covered venue's demand before assigning capital. A small
probe estimates quantity, followed by a full-size mark to validate depth. It remains a
comparison policy and may cover zero complete routes. Both policies return exact
original-unit `requirements`, per-route `funded` flags and an explicitly calculated
`funded_route_count`. That count means alternative inventory coverage at retained
projected sizes; it never means current profitability, eligibility, concurrent
availability or successful simulated execution.

CAD identity is exactly one. Every other funded asset requires a fresh collector CAD
mark with source timestamps, full quantity/depth evidence, FX provenance and confidence.
No stablecoin parity or unsupported conversion is used. Quantities and sourced rates
round down at 18 decimal places; contributed CAD values use the existing accounting
half-even NUMERIC convention. Values exceeding the capital budget are rejected. All
rounding, missing-depth and unused allocation remain CAD cash. Values sum exactly to
starting capital, including the reserve. Tiny non-CAD positions cannot acquire a zero
cost basis.

The result is an immutable proposal with a five-minute expiry. The basis is the sourced
initial inventory contribution, not a fictional exchange purchase/fill. Research
valuation may use unverified/reference-venue marks; all such evidence remains explicitly
hypothetical and excluded from validated performance.

## Creation, comparisons and reset

`POST /api/v1/paper/funding/experiments` takes either a proposal ID or explicit
allocations, a unique idempotency key and a RESEARCH_SHADOW strategy. Custom non-CAD
allocations require a positive CAD valuation; owner-supplied assumptions remain
research-only. Starting values must equal the requested capital exactly. A user row lock
and immutable key/fingerprint receipt prevent duplicate funding on retry; reusing a key
with different contents is rejected. A proposal's target trade budget is applied to the
created strategy; an explicitly mismatched strategy is rejected. Existing session locks,
journal balance guards, active-session caps and scope isolation remain in force.

Comparisons should use equal starting capital and contemporaneous evidence, with
benchmark/exposure differences disclosed. `reset_from_session_id` requires an owned
research session and the same initial capital; it creates a new session and never
changes the source. The immutable initial holdings define a separate no-trading
benchmark for every experiment.

