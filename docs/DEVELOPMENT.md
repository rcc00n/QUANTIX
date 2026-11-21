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

## Frontend and browser verification

Node.js 22 and npm:

```bash
cd frontend
npm ci
npm run typecheck
npm run format:check
npm run build
npx playwright install chromium
npm run test:e2e
QUANTIX_LIVE_E2E=1 npm run test:e2e
```

Browser tests use the local owner credential file or QUANTIX_E2E_USERNAME /
QUANTIX_E2E_PASSWORD environment values. Traces are disabled; do not enable secret-
containing debug traces or point tests at public production. The opt-in live test
checks actual normalized bid/ask records from the current private API and real brand
assets; it does not install synthetic data. Base URL override: QUANTIX_E2E_URL.
PLAYWRIGHT_CHROMIUM_EXECUTABLE may select an existing local Chromium installation.

For frontend host development:

```bash
BACKEND_URL=http://127.0.0.1:8000 npm run dev
```

The server-only BACKEND_URL is not embedded in client bundles. Dashboard rendering
and all backend routes independently verify database sessions.

## Test boundaries

Synthetic backend tests cover financial primitives, UTC and report contracts,
authentication/rotation/history preservation, discovery and asset identity, capabilities,
exact L2 decoding, malformed/crossed/shallow/stale books, reference confirmation,
clock drift, bounded cache/queue, global/per-exchange rate limits, Retry-After,
timeouts/backoff, checksum/sequence gaps, disconnect/new-snapshot recovery,
PostgreSQL upserts/restart idempotency, transient retention and immutable financial
history. API integration tests cover protected catalogs/books, stale fallback and
unavailable collector responses. No test mocks count as live exchange verification.

Actual live checks are separate:

```bash
make live-check
make collector-logs
```

Reports show measured adapter/venue counts, market coverage, sources and timestamps.
A historical successful public book does not imply that its exchange is currently
healthy or legally actionable. See docs/LIVE_VERIFICATION.md for the stage snapshot.

