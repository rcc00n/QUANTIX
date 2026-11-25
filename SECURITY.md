# Security policy

QUANTIX is a paper-trading research application. Authentication, financial evidence
and private exports still need protection. The maintained public source is `main`;
older reconstructed import checkpoints are not supported releases.

## Report privately

Use [GitHub private vulnerability reporting](https://github.com/rcc00n/QUANTIX/security/advisories/new).
If that route is unavailable, contact the maintainer at **vadrud2016@gmail.com**
with the subject `QUANTIX security report`. Do not post exploitable details or
sensitive material in a public issue. No response-time guarantee or bounty is offered.

Include the affected commit/version, a short impact description and minimal
reproduction using synthetic data. Redact tokens, cookies, credentials, personal
information and private financial records. Do not attach production exports or
secret-bearing logs. Never test against someone else's account or infrastructure.

## Safe local investigation

Use an isolated checkout and disposable local data; follow
[development](docs/DEVELOPMENT.md) and [contribution guidance](CONTRIBUTING.md).
Keep services on loopback, preserve financial evidence and do not start a duplicate
Telegram consumer. Real exchange orders are outside this project's authorized mode.
