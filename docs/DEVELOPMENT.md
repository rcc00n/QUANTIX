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

## Backend outside Docker

Use Python 3.12+ and uv:

```bash
cd backend
uv sync --frozen
uv run ruff check arbiq ../tests
uv run pytest -q
```

Without TEST_DATABASE_URL, local pytest runs unit/contract tests and explicitly skips
PostgreSQL integration tests. `make test` is the complete acceptance suite, with no
SQLite substitution. Custom integration databases must end `_test`; other names are
refused. All financial/market fixtures identify themselves as synthetic.

For host development, supply DATABASE_URL pointing to a separately managed private
PostgreSQL, ADMIN_USERNAME, ADMIN_PASSWORD_HASH and other environment values. Do not
publish the Compose database. Alembic reads DATABASE_URL from the process environment;
FastAPI/Pydantic can read an ignored .env in its working directory.

```bash
uv run alembic upgrade head
uv run uvicorn arbiq.main:app --host 127.0.0.1 --port 8000 --no-access-log
# Separate terminal/process; requires COLLECTOR_INTERNAL_TOKEN and current schema:
uv run python -m arbiq.collector.worker
```

In host development set COLLECTOR_URL=http://127.0.0.1:8001 and protect local service
access. The supported Compose worker exposes no host port and uses an internal token.

