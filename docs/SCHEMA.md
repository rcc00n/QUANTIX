# PostgreSQL financial model

Schema revision: `0010_notifications`. Ten additive migrations define 56 tables and their audit guards.
UUID identifiers are assigned by application code; created_at defaults to PostgreSQL
now(). Every timestamp is TIMESTAMPTZ; contracts require explicit UTC. Every financial
scalar is NUMERIC(38,18). JSONB financial scalars must be decimal strings. FK deletion
defaults to RESTRICT/NO ACTION, never cascading away evidence.

## Four distinct records

| Concept | Tables | Meaning |
| --- | --- | --- |
| Opportunity | opportunities + referenced books/fees/FX/strategy | An estimated possibility; never a fill or guarantee |
| Alert | signals decision=alert; notification_attempts for delivery | Passed notification rules; delivery may still fail |
| Paper trade | paper_trades/orders/fills/simulation_events | A virtual outcome with delay, inventory and provenance |
| Actual trade | actual_trades only | Explicit user-confirmed real activity; no paper-trade FK or write route |

## Record groups

| Tables | Purpose and provenance |
| --- | --- |
| users, auth_sessions, login_attempts | Operator identity, hashed session tokens, expiration/revocation, durable throttling |
| exchanges | Public adapter identifier, enabled flag, capability metadata, configuration version |
| assets, markets | Canonical identity/network; exchange-native pair; spot precision, increments and minimum notional |
| exchange_config_versions | Immutable exchange capabilities/public configuration per version, effective time and change reason; no credentials |
| strategy_versions | Immutable owner/version/settings/reason history; all later decisions reference it |
| fee_versions | Effective fee fractions, scope, source, tier/fee currency assumptions; rebates permitted for maker fees |
| fx_quotes | Explicit base→quote rate (quote units per base unit), source, observed_at and provenance |
| market_observations | Timestamped top-of-book, sizes, receive time and explicit quality/reason |
| order_book_snapshots | Full relevant bid/ask levels as decimal-string arrays, source, sequence, observed/received times and content hash |
| opportunities | Both market/book/fee references, FX and strategy versions, quantity, estimated gross/fees/expenses/net CAD and net percentage, expiry and calculation version |
| signals, notification_attempts | Decision/reason/rule snapshot; channel, attempt, status, delivery timestamp/provider ID and sanitized error code |
| telegram_challenges, telegram_bindings | Hashed short-lived owner enrollment, explicit dashboard approval/revocation and private destination; excluded from exports |
| notification_worker_state | Singleton worker heartbeat, safe public bot identity and durable incoming update offset |
| alert_decisions, alert_lifecycle | Immutable source/settings/rule decision plus mutable acknowledgment/cancel/repeat control, separate from financial executions |
| notification_outbox, notification_events | Durable delivery scheduling/status/expiry and immutable attempts/events; ambiguous sends are not silently retried |
| paper_sessions | Strategy, virtual initial CAD basis, initial inventory/FX benchmark snapshot, session lifecycle |
| virtual_accounts | Session + exchange + asset + role; system equity/expense accounts may omit exchange; NULLS NOT DISTINCT uniqueness |
| journals, ledger_entries | Idempotent event, effective time, source UUID and provenance; signed asset quantity/CAD annotation per account |
| virtual_deposits | Initial/subsequent virtual funding, exact quantity, FX and CAD equivalent linked to a journal |
| balance_snapshots | Account quantity at an instant and last included journal; a checkpoint, never the source of truth |
| paper_trades, paper_orders | Attempt outcome, reaction delay and eligible time; two legs, limits, side, requested quantity and failure reasons |
| paper_fills | Individual partial fill, price/quantity, actual evaluation book, execution time, fee currency/quantity/CAD, fee/FX version, cost basis, realized simulated P&L and provenance |
| simulation_events | Append-only transition/rejection evidence, timestamps and failed/missed reasons |
| inventory_transfers | Virtual movement, settlement timing/state, expense asset/quantity/CAD and journal/provenance |
| portfolio_valuations | Equity, realized simulated P&L, unrealized inventory P&L, fees, expenses, utilization, drawdown, benchmark and external flows; complete valuation inputs and calculation version |
| actual_trades | User, market, side, quantity, price, execution and explicit confirmation times, reference/evidence; never inferred |
| system_events | Append-only connectivity, quality, auth/config/system audit; exchange/actor linkage and sanitized details |
| report_exports | Versioned period, owner, status, artifact SHA-256 and provenance for later exports |

## Ledger invariants implemented in PostgreSQL

- Journal + entries must be inserted in one transaction. Each journal requires at
  least two entries and zero signed quantity total **per canonical asset**.
- All accounts in a journal must belong to its paper session. USD cannot offset
  BTC, and separate paper sessions cannot fund one another.
- Deferred constraint triggers check final transaction contents at COMMIT.
- Entries cannot be appended to an already committed journal, even as a balanced pair.
- Ledger, journals, virtual accounts, deposits, balance checkpoints, fills, valuations,
  source books/observations/FX/fees, strategy/exchange configuration versions, opportunities, signals,
  simulation/system events and actual trades reject UPDATE, DELETE and TRUNCATE.
- Corrections use new compensating journals/evidence, never rewriting history.
- A virtual deposit posts +quantity to exchange inventory and -quantity to the
  same asset's equity account. Reservations move between available/reserved roles.
  Trades post each asset independently against appropriate counteraccounts.
- CAD annotations explain valuation, not interchangeable units. Services in Stage 4
  must reconcile CAD annotations, reserve inventory, prohibit overspending, validate
  order/fill sums, verify same-asset transfer endpoints and lock accounts atomically.
  DB structural invariants do not constitute a working accounting service.

## Indexing and retention

Created-at indexes support audit/report windows. Composite market/time indexes support
book replay; session/time indexes support portfolio history. Foreign-key and ledger
account/journal indexes cover critical joins. Add workload-driven indexes, partitioning,
archival and retention budgets after real collection volume is measured in Stage 2/7.
No destructive cleanup job is enabled.

## Reconstruction requirements for future writers

Store source IDs and decimal-string assumptions for fee currency, tier, CAD FX,
slippage reserve, rebalancing reserve, observed liquidity, rounding, stale/skew gates,
requested/eligible/evaluated/fill times, cost basis policy and calculation version.
On any failed/missed attempt, write a reason and evidence even when there are no fills.
A partially completed two-leg trade retains residual inventory and unrealized risk;
it is never counted as a fully realized arbitrage. Settings changes append versions.

Additional columns and constraints are defined in arbiq/db/models.py and frozen in
Alembic migrations. New stages must extend migrations rather than editing applied ones.

