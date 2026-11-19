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

