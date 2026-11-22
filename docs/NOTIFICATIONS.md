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

