# Urgent nighttime notifications — implemented Stage 6 policy

Current implementation and proof are in [NOTIFICATIONS.md](NOTIFICATIONS.md).
Telegram uses a separate singleton worker and private owner-approved pairing.
A configured bot identity does not establish private device delivery. Paid push
and voice stay disabled. AI cannot choose thresholds or override rules.

Dispatch consumes persisted decision IDs with explicit expiry, quantitative
CAD/net-percent/liquidity/confidence gates and idempotent deduplication keys. Each
channel delivery, repetition, cancellation, escalation, failure and acknowledgment
must have a PostgreSQL audit record. A delivered message is not a paper execution.

- Timezone: America/Edmonton using zoneinfo and local wall-clock quiet hours;
  handle daylight-saving transitions explicitly. Defaults are 23:00–07:00.
- Sleeping-hour expected CAD profit and expected net percent thresholds must be
  at least as high as daytime thresholds. Minimum liquidity retains its quote units
  until a documented CAD FX conversion exists. Require fresh HEALTHY data and
  validated quantitative opportunities from Stage 3.
- Repeated emergency push (e.g. Pushover) requires a provider delivery receipt,
  expiration time, retry budget and acknowledgment polling/webhook signature checks.
- Continue only until acknowledged, expiry, cancellation, maximum repetitions or a
  configured cooldown. Re-evaluate expiry and opportunity confidence before every send.
- Manual acknowledgment must authenticate the actor, hash opaque tokens at rest,
  and atomically cancel pending repeat/escalation jobs across channels.
- Deduplicate per opportunity + strategy version + urgency window; escalation
  cooldown prevents a failed channel from causing phone/push storms.
- Optional outbound calls remain disabled by default. Enabling a paid push or
  voice API requires explicit owner authorization and a Stage 6 implementation.

Configuration placeholders in .env.example:

| Channel | Configuration | Required before activation |
| --- | --- | --- |
| Telegram | TELEGRAM_TOKEN_FILE and private dashboard-approved pairing | Explicit opt-in, private ownership challenge, authenticated approval, sanitized delivery logging |
| Emergency push | PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, URGENT_ALERTS_ENABLED=false | Explicit cost authorization, emergency receipt/cancel/ack support, quotas |
| Voice | VOICE_PROVIDER, VOICE_API_KEY, VOICE_FROM_NUMBER, VOICE_TO_NUMBER, VOICE_CALLS_ENABLED=false | Explicit cost authorization, destination validation, call limits, consent and cancellation |
| Policy | ALERT_TIMEZONE=America/Edmonton; versioned UrgencyPolicy | Quiet hours, higher sleep thresholds, expiry, liquidity/confidence, repeats and cooldowns |

The providers are replaceable through UrgentProvider. Migration0010 adds explicit
alert decisions/lifecycle, outbox/events and private pairing/worker state; legacy
notification_attempts remains preserved. See NOTIFICATIONS.md for tested Telegram
retry/expiry/acknowledgment, quotas and conservative ambiguous-send behavior.
