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

