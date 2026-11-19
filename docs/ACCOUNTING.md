# QUANTIX paper accounting and simulation

Stage4 is strictly virtual. No order-placement client, exchange trading key, paid
provider or real-trade writer exists. ActualTrade remains entirely separate.

## Capital and scenario isolation

The default validated portfolio contributes C$10,000 cash, split equally between
Kraken and NDAX virtual accounts. It cannot trade merely because public data works:
fees, provincial/account eligibility, funding and host-clock conditions remain
independent gates. Current Stage3 actionable=false routes therefore cannot become
validated trades.

A separate, explicitly hypothetical research portfolio has its own C$10,000 initial
capital. It is not added to the validated portfolio. When trusted observed BTC/CAD
marks are available, half its capital is contributed as priced BTC inventory and
half as CAD across Kraken/NDAX. Crypto quantities times the initialization rate
consume that capital; remaining fractional CAD stays cash. These are initial
inventory contributions, not fabricated exchange purchases. If evidence is missing,
research initialization waits. Other assets and allocations can be supplied through
the private portfolio API; assumed prices require research classification.

Classification cannot be changed after funding, including by direct database
updates. To change starting equity or switch between validated and research modes,
create another portfolio. Existing history is preserved. Each simulation pins its
immutable StrategyVersion and original settings; editing a strategy affects later
candidates, not already reserved trades.

## Ledger and moving basis

Every physical asset movement uses immutable Journal and LedgerEntry records:

- Funding credits exchange available inventory and debits same-asset equity.
- Reservations move units from available to reserved; they create no profit.
- Each virtual leg balances base and quote independently against counteraccounts.
- Transfers consume source inventory, credit destination inventory and disclose
  explicitly modeled network/withdrawal costs. They never create destination units
  before the configured settlement delay.
- Releases return only the unused units owned by that reservation.

A transaction locks its PaperSession before checking inventory/idempotency. A
reservation belongs to its trade; another trade cannot spend its reserved pool.
Ledger-derived available and reserved quantities must reconcile to PortfolioPosition.
Database deferred guards prohibit negative available/reserved inventory and enforce
per-asset balance, same-session entries and sealed append-only journals. Fill guards
check journal/session, snapshot/market and fee-version provenance.

All monetary arithmetic uses Decimal with private78-digit contexts. SQL financial
values use NUMERIC(38,18). Original unrounded evidence stays in JSON as decimal strings.
Internal derived quantities are floored to exchange steps before storage validation;
buy debits/fees round conservatively upward, sale proceeds downward. Inputs reject
binary floats. No global decimal-context change affects other tasks.

Weighted average CAD cost basis is maintained independently per venue/asset.
Acquisition fees enter bought-asset basis. A sale realizes net CAD proceeds less
removed basis. Spending a foreign quote inventory realizes its separate FX basis
change. Unknown CAD rates propagate NULL basis/P&L; they are not zero.

Portfolio reconciliation is:

`equity − contributed capital = realized disposal P&L + realized FX P&L + unrealized inventory P&L`.

Fees, modeled slippage and paid transfer expenses already change cashflows/basis;
their disclosure columns are not subtracted a second time. Selling an appreciated
initial coin realizes some holding gain, so realized disposal P&L must not be called
pure arbitrage profit. The unchanged-initial-holdings benchmark and strategy excess
separate performance from market appreciation. Benchmark depth is valued for the
original quantity, separately from today's changed inventory quantity.

Per-trade matched-leg cashflow P&L is a separate metric. Only fully filled,
inventory-matched completed simulations with known CAD values enter its win rate.
Partial/unmatched positions remain portfolio exposure; they do not manufacture a
closed arbitrage profit. No completed observations means null win rate/average,
not an invented success statistic. Hypothetical research never enters validated
primary totals.

## Time, independent legs and failure

Lifecycle: CANDIDATE → ELIGIBILITY_CHECK → SCHEDULED → RECHECKING → EXECUTING →
COMPLETED/PARTIAL/MISSED/FAILED/EXPIRED. Immutable SimulationEvents retain transitions.
Default reaction delay is30seconds;5/15/30/60/120/custom are supported. Fast/manual
profiles are counterfactual simulation settings, not measured real exchange latency.
An explicit UTC-uncertainty buffer and raw monotonic deadline prevent early execution.

The collector captures the first valid available book after each order's deadline.
It pins source/ingestion UTC, causal ingestion index, complete depth, clock uncertainty
and available FX. Receipt cannot come after the decision. Missing source timestamps
or extreme anomalies require an explicitly permissive research profile; malformed,
crossed, stale, disconnected and untrusted-UTC data never create fills.

First-leg preflight uses only the opposing book available by that observation, never
later evidence. Disappeared spreads/limits/threshold failures cancel preflight.
Orders then execute independently, one journal/fill per committed transaction. The
second leg can fail, fill partially or time out. Processing uses earliest captured
availability; late out-of-order evidence is rejected rather than costed with future
inventory basis. Restart reuses pinned evidence, unique trade/fill keys and owned
reservations, preserving completed legs without repeating balances.

Adverse slippage is an explicit modeling assumption applied to observed VWAP,
retained separately from the observed price. It is never represented as an actual
exchange fill. Future rebalancing reserves affect candidate approval; actual simulated
rebalancing expenses enter the ledger only when the configured transfer is performed.

Transfer scenarios buy first, reserve the acquired base while in flight, wait for
configured network/confirmation/settlement assumptions, charge configured costs,
reserve credited destination units and require a genuinely new selling book after
settlement. The original sale quote is never guaranteed. A configurable haircut is
pretrade scenario risk, not an extra adjustment to the later observed sale price.
Networks/costs remain unverified unless explicitly sourced; exploratory assumptions
stay isolated in research. Rounding dust remains unmatched inventory, not closed P&L.

Maximum concurrency, per-venue allocation, inventory exposure, loss, hourly activity,
asset/exchange filters and AND/OR profit thresholds are enforced before reservation.
The global pending budget bounds work. Loss controls cannot guarantee a future price
or prevent an already-open position from losing value; its later marks remain visible.
Pause stops new scheduling; existing reserved legs continue with an audit trail.

## Valuation and analytical interfaces

PortfolioCheckpoint is immutable, normally written every60seconds. Live reads can
compute an unpersisted checkpoint. Marks use observed liquidation depth and explicit
CAD conversions; unavailable/stale FX or depth yields incomplete valuation. A retained
snapshot is explicitly labeled and never presented as fresh.

DailyAccountingSnapshot revisions use America/Edmonton, including DST, with an
explicit as_of. The worker creates current-day snapshots hourly/on local day change.
They are provisional numerical daily/cumulative views, not a falsely exact midnight
close. Data limitations, late processing and missing marks remain explicit.
Financial history is never pruned by transient market-data retention.

Private endpoints include `/paper/sessions`, per-session `/portfolio`, `/metrics`,
`/daily`, `/strategy`, `/pause`, `/resume`, `/rebalance`, `/paper/trades` and trade
details. Exact decimal strings, classification, versions, orders/fills/events,
mark/fee/FX provenance and null-value reasons support independent recalculation.
Queries are paginated/bounded; numerical totals use SQL aggregates over full periods.
Stage5 analytics and the complete authenticated ZIP now consume these JSON/ledger
interfaces; see REPORTING.md and TERMINAL.md. AI text never modifies records or
supplies numerical conclusions.

## Verification and remaining scope

All financial/execution fixtures are explicitly synthetic and use an isolated `_test`
database. Live owner portfolios use actual public observations and genuine virtual
state; no fixture fill is inserted into the application database. Resource/stress
fixtures make zero external API requests. See docs/PERFORMANCE.md and VPS_AUDIT.md.


Stage5 valuation selects timestamped source evidence. If a holding venue omits its
source timestamp, a fresh independently timestamped reference may be used with
verified=false and reference_venue provenance; it never proves executable liquidity
on the holding venue. No timestamp is invented. If no such reference exists, equity
and benchmark remain unavailable. Pending lifecycle reasons are separate from
terminal missed/failed/expired/partial reason counts.

