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

## Financial inputs and confidence

See [FINANCIAL_ENGINE.md](FINANCIAL_ENGINE.md) for exact equations and optimizer
limitations. All financial arithmetic is Decimal with a private 78-digit context.
The scanner uses marketable taker assumptions, observed depth, precision/amount and
notional limits, separate native currencies, executable CAD conversions and explicit
slippage/rebalancing reserves. No depth extrapolation or stablecoin parity exists.
The buy budget excludes the separately reported pre-funded selling inventory;
both venue capital requirements and projected inventory changes are disclosed.

Default fee estimate per leg is max(0.6%, advertised public taker metadata). Account
fee tier, fee currency and future rebalancing expense are unverified. The scanner
assumes quote-currency fees and reserves 10bps slippage plus 10bps rebalancing, all
clearly labeled unverified. These configurable estimates are not exchange guarantees
or worst-case cost bounds. Fee versions preserve the policy and advertised value.
Unknown critical fees are never represented as zero or verified.

CAD is an identity conversion. Other quotes require fresh executable currency books
among CAD/USD/USDC/USDT/EUR, at most two hops, with acquisition asks and liquidation
bids, observed depth and conversion fees. Conversion timestamps, hashes, routes and
book evidence are pinned. Conversion uses public reference venues; actual funding
and account access are unverified. Missing/stale/insufficient conversion evidence
suppresses CAD profit. Native same-quote calculations may remain visible, explicitly
without an applied CAD budget. Crypto legs are not reused as FX depth. A depeg changes
observed conversion rates instead of being overridden with one USD.

Pre-funded calculations assume buying quote on one venue and selling base on the
other; they do not infer balances. Transfer scenario calculations require separately
supplied network/withdrawal/deposit/confirmation/timing assumptions, charges and a
future-price haircut. Live transfer results remain unavailable while those inputs
are unverified. Current sale prices are never guaranteed after a transfer delay.

