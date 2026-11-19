# QUANTIX Stage 3 scanner

The scanner is a separate asyncio task inside the singleton collector process. It
shares the validated cache and bounded REST recheck scheduler, not an exchange
trading client. It never creates orders, fills, ledger entries or alerts. Stage 4
will manage virtual funding, execution delays and accounting.

## Coverage and scheduling

Discovery indexes canonical base/quote keys by independent venue group in linear
catalog time. Only actively monitored compatible markets are compared. Coinbase and
Coinbase Exchange share one venue group and never compare against each other.
Unknown, wrapped and synthetic identities remain isolated. Market selection reuses
Stage 2 filters and ranks CAD/conversion anchors, cross-venue coverage, configured
asset priority, quote preference and available native-quote volume. The default cap
is ten markets per adapter (configurable 1–50), not thousands of subscriptions.
Catalog-wide allocation is reconsidered every minute without rediscovering APIs.
The existing liquidity-adaptive refresh, weighted rate limits, retry/backoff,
concurrency and Kraken WebSocket controls remain in force.

Changed books coalesce by canonical base; conversion-book changes also revalue
related routes. The active index bounds comparisons to monitored markets. Metrics
separate discovered compatible keys, monitored keys, unordered venue intersections,
directed comparisons, evaluations and detections. They never equate discovered
listings with monitored books. Screening uses ask/bid prices, never last prices.

The scanner requests fresh REST evidence through the collector's existing queue32,
30-second per-market cooldown and exchange backoff. Rechecks never bypass rate
limits, imply execution or promise that a spread persisted. Extreme repeatable
outliers retain snapshots, independent references and classification; disagreement
alone does not erase a possible genuine dislocation. Invalid structure, stale source
or ingestion timestamps and disconnected sources prevent verified classifications.

