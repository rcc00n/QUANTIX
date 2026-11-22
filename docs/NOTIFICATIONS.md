# QUANTIX notifications

Stage 6 runs an independent lightweight Telegram process. It does not place orders,
write balances, change scanner decisions, require AI, or enable paid providers.
All money thresholds are exact Decimal strings.

## Startup and credentials

The owner-provided bot token is read from `.secrets/telegram-bot-token` (0600 on the
host) through a read-only Docker secret at `/run/secrets/telegram_bot_token`.
Only the notifications container receives this secret. Never put the token into a
URL in a report, a command argument, logs, an export, source control, or screenshots.
`TELEGRAM_TOKEN_FILE` changes the file location; `NOTIFICATION_WORKER_ENABLED=true`
allows the standalone worker. Core API and collectors work without this worker.

After migrations, `docker compose up -d notifications` starts
`python -m arbiq.notifications.worker`. Its dedicated PostgreSQL advisory connection
holds lock 7271142606; losing that connection stops its consumer. Worker pool size
is two with one overflow connection. A second worker exits. The worker checks
`getMe` and `getWebhookInfo` before polling and never deletes another webhook.
Telegram 409 consumer conflicts stop polling until operator review and restart.
Do not run another `getUpdates` consumer for this bot.

Health is persisted in `notification_worker_state` and written to the private
`/tmp/quantix-notification-health.json` file. Compose verifies both provider health
and a recent CLOCK_MONOTONIC_RAW heartbeat. Missing tokens produce explicit degraded
health without stopping market collection or paper trading. The worker independently
corroborates UTC with the existing TrustedClock and fails closed when trust expires.

## Owner pairing

1. Log into QUANTIX and create a challenge in Settings.
2. Open the returned Telegram deep link, or send `/start <challenge>` privately.
3. Return to the authenticated dashboard, inspect the requesting identity, and approve.
4. Enable Telegram policy and use **Send test** if desired.

Challenges expire after ten minutes, are hashed at rest, require a private chat with
matching Telegram sender/chat identity, and can be consumed only once. Starting a new
challenge invalidates the previous challenge. Only the authenticated initiating owner
can approve. Arbitrary `/start` messages cannot enroll an account or reveal information.
Unpaired users receive no private replies. Revoke pairing in Settings to cancel queued
messages. Challenge, token and raw Telegram destination IDs are excluded from exports.

Paired private commands are `/status`, `/radar`, `/paper`, `/portfolio`, `/alerts`,
`/pause`, `/resume` and `/help`. They query genuine database state. Retained scanner
and valuation timestamps remain visible; unavailable values remain unavailable.
Pause/resume affect notification delivery, independently of paper trading.

## Rules and evidence

The minimum **actual executable Buy Capital** defaults to C$50. This uses immutable
`ArbitrageEvaluation.acquisition_cost_cad`, including the purchase's modeled costs,
independently of selected maximum budget, expected profit and percentage thresholds.
Every monetary threshold must pass (AND). Quantities and sell-side prefunded inventory
must be positive and known. The minimum liquidity CAD gate measures the depth-supported
executable acquisition amount; native book depth and conversion assumptions remain
in the referenced evaluation. Small research observations are retained unchanged.

Eligible actionable alerts require every explicit eligibility, funding, fee, FX,
market-limit and canonical-identity confirmation, HEALTHY books, trusted and healthy
original clock evidence, current UTC trust, non-future fresh source/ingestion times,
positive net profit, unexpired route evidence, route cooldown and owner policy.
The source UTC uncertainty is read from preserved `uncertainty_ms`/seconds evidence;
a conservative two-second bound for the current trusted UTC is added when checking
age. Thresholds are never relaxed to compensate for an unreliable host clock.
Existing research-only scanner routes remain non-actionable.

Optional ordinary research delivery defaults off and always says **RESEARCH ONLY**,
includes missing confirmations, and cannot trigger emergency repetition, push or voice.
`telegram_enabled` defaults false and secure pairing alone does not enable alerts.

Quiet hours use America/Edmonton zoneinfo rules, default 23:00–07:00. Ordinary messages
are suppressed by default, or may be silent. Emergency rules default to C$250 minimum
Buy Capital, C$500 depth-supported capital, C$25 profit, 1% return, maximum three-second
quote age and ten seconds of independent fresh REST-confirmed persistence. All gates
must pass. Repeats default off; when enabled, they stop on acknowledgment, cancellation,
expiry, missing fresh confirmation, repeat budget or daily cap. Each repeated message
pins its source evaluation and revalidates both that evidence and the current route.
Daily cap defaults five messages; route escalation cooldown defaults ten minutes.

Alert decisions and notification lifecycle events are immutable PostgreSQL evidence.
Mutable outbox/lifecycle projections preserve delivery status without modifying orders
or the financial ledger. Each source, configuration version, book and fee reference
is retained. Authenticated history, decisions and events expose the delivery audit.
The scanner remains separate: a Telegram alert is neither a fill nor simulated P&L.

## Delivery semantics

The durable outbox has unique deduplication keys and at most 1,000 active jobs. The
worker reads at most three deliveries per tick, 25 updates per poll, 32 current source
evaluations per scan and ten due repeat jobs. Historical financial data is never pruned.
Polling updates and their resulting queued command replies commit in one transaction.
Delivery uses plain text, protected content, no link previews, one message per 1.1 seconds,
explicit 429 server backoff, bounded exponential retries, and message expiry.

Telegram does not provide an idempotency key for `sendMessage`. A network interruption
after sending or a restart with an in-flight request is recorded as **ambiguous**.
Such messages are not automatically resent; they may already have reached the owner.
An explicit provider rejection can retry, with at most eight attempts. No exactly-once
network-delivery claim is made. Acknowledgment/revocation cancel queued work; an already
in-flight Telegram request can still arrive. Expired-before-delivery rows are counted.
Messages show observation time and warn that historical prices may no longer execute.
Mobile alert text formats CAD amounts to two decimal places, returns to at most four
places and native prices/units to at most eight places. A rounded value carries `≈`;
tiny nonzero units use scientific notation rather than appearing as zero. The linked
portal preserves exact original values, and no display rounding feeds a rule or ledger.
No Telegram message is guaranteed to bypass a phone's Focus mode or wake its owner.

`DisabledPaidProvider` implements push/voice boundaries and rejects all delivery calls.
No Pushover, Twilio, voice call, subscription or paid API is enabled by preferences.

