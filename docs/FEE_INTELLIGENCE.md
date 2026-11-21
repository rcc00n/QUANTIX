# Spot fee intelligence

The Stage 9 catalog was reviewed on 2026-11-24 at 05:49 UTC. Exchange fee schedules describe product and region scope. The scanner uses taker fees because the simulation consumes resting depth. The account tier, discounts, spot access and Alberta eligibility require distinct evidence.

Each fee assessment records the applied Decimal rate and denomination, configured floor, available market-metadata rate, public tier and schedule revision, source links, review time, source effective date when stated, and any unresolved commission requirements. Tier thresholds use the source units. The linked immutable fee rows include known public tier evidence in the existing `assumptions` field. Resize and replay consume pinned historical calculation inputs. Newly collected evidence never reprices preceding paper fills or journal entries.

- **PUBLIC_SCHEDULE_VERIFIED** applies to a causal numerical schedule in the reviewed product scope. Public pricing and estimated calculation costs remain separate fields.
- **UNKNOWN** means the numerical schedule is unavailable or outside its scope, including historical decisions before the recorded review and account-gated products.
- **STALE** applies to a reviewed numerical schedule older than 90 days. Retaining the last known public rate as a conservative floor prevents a favorable modeled fee drop when evidence expires.
- **CONSERVATIVE_ESTIMATE** is the applied pricing class. The rate is the maximum of the configured floor, valid advertised taker rate and causal standard public taker rate. The default floor is 0.60%.
- **ACCOUNT_VERIFIED** is reserved for an authenticated, product-specific account verification. This release has no such verified account evidence.

## Numerical public coverage

| Adapter | Published baseline maker / taker | Product scope |
|---|---:|---|
| Kraken | 0.40% / 0.80% ordinary spot and 0.20% / 0.20% stablecoin-base or FX | The ordinary base tier raises the former 0.60% default estimate. Kraken names a fee-precision-dependent minimum; amount and account fee-denomination preference remain unresolved. [Kraken Pro](https://www.kraken.com/pro/fees), [fee rules](https://support.kraken.com/articles/201893638-how-trading-fees-work-on-kraken) |
| Coinbase Exchange | 0.40% / 0.60% normal and 0% / 0.0045% qualifying Stablepairs | Uses the native product flag and explicit documented USDT/USD and USDT/USDC exclusions. No Liquidity Program rate is assumed. [Exchange fees](https://help.coinbase.com/en/exchange/trading-and-funding/exchange-fees) |
| NDAX | 0.20% / 0.20% | Flat Canadian schedule. Fees are charged in the received asset, so a buy fee reduces base received while a sell fee reduces quote received. [Fee schedule](https://ndax.io/en/fees), [Canadian disclosure](https://ndax.io/en/legal/account-agreements/relationship-disclosure-document) |
| Gemini | 0.60% / 1.20% ordinary spot | ActiveTrader and exchange API, charged in quote currency. The ordinary base tier raises the former 0.60% estimate. Three explicitly listed zero-fee pairs retain the research floor. [ActiveTrader schedule](https://www.gemini.com/fees/activetrader-fee-schedule) |
| Bitget | 0.10% / 0.10% | Standard spot, with selected VIP volume and balance thresholds retained. [Fee table](https://www.bitget.com/fee/) |
| Bitfinex | 0% / 0% | Explicit published spot rate from 2025-12-17. The existing positive research floor still applies. [Official fee notice](https://support.bitfinex.com/hc/en-us/articles/213919589-What-fees-does-Bitfinex-charge) |
| Crypto.com | 0.25% / 0.50% | Standard Exchange spot without CRO benefits. [Exchange fee schedule](https://crypto.com/exchange/document/fees-limits?tab=2) |
| Binance | 0.10% / 0.10% | Global regular spot, before fee-token discounts, promotions or USDC preferences. [Trading fees](https://www.binance.com/en/fee/trading) |
| OKX | 0.08% / 0.10% | Ordinary crypto/USDT Group 1 scope. The documented received-asset fee treatment is used for buy and sell legs in scope. Regional and special groups remain unresolved. [Group 1 rates](https://www.okx.com/en-gb/help/important-notice-consolidation-token-pairs), [fee rules](https://www.okx.com/help/trading-fee-rules-faq) |
| Bybit | 0.10% / 0.10% | Public crypto-to-crypto base rates. Special and regional groups can differ. [Trading fee structure](https://www.bybit.com/en/help-center/article/Trading-Fee-Structure?category=top) |
| Bitso | 0.30% / 0.36% on USDC digital-dollar markets and 0.60% / 0.78% on MXN markets | Bitso International, with quote-scoped tiers. The USD label in the schedule does not establish an asset identity or conversion peg. [Trading fees](https://bitso.com/fees?hsLang=es-mx) |

Coinbase Advanced provides account-dependent fee methodology and requires an authenticated account for the numerical tier. The Coinbase Exchange public rates are not copied into Advanced. Bitstamp, KuCoin, Gate, MEXC, HTX, Poloniex, Luno and bitbank currently use the conservative policy and any admissible market metadata without a pinned numerical public schedule. [Coinbase Advanced fees](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees)

For a future account-specific verification, confirm the exact adapter product, jurisdiction and account type. Record the pair, maker/taker tier, fee asset, minimum and rounding rules, effective date, verification timestamp and source. Use the official account interface or a redacted export that avoids credentials and private account identifiers. Existing paper strategies preserve their explicit research fee-currency assumptions and use the larger of their configured fee rate and the route’s pinned fee rate.
