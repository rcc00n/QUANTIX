# QUANTIX development and verification

## Supported local stack

`make setup` creates credentials on a fresh checkout and preserves an existing .env.
`make up` builds db/migrate/backend/collector/frontend with health-gated dependencies.
`make test` recreates only the disposable test-db and runs the complete backend suite.
Application PostgreSQL data stays in the existing arbiq_postgres_data volume.
The Makefile disables Docker Compose 2.40.x Bake to avoid a host tooling crash.

The configured owner is quantix. The generated password is available only through
.secrets/admin-password (0600). `make reset-password` generates a new passphrase,
updates the environment atomically and restarts the backend to revoke old sessions.
The Stage 2 admin→quantix rename preserves the original user ID, not a second active
legacy login. No credentials belong in progress documents, screenshots, CI output,
application logs or report exports.

