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

