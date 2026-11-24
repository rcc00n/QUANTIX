# Contributing to QUANTIX

Read [AGENTS.md](AGENTS.md), [PROGRESS.md](PROGRESS.md), the
[roadmap](docs/ROADMAP.md) and the relevant module contract before changing behavior.
Work within one requested stage and preserve existing functionality and evidence.

## Financial changes

Use Decimal and PostgreSQL NUMERIC for financial values. Preserve causal timestamps,
source provenance, independent experiment capital and append-only journals. Include
focused tests for every changed critical calculation and identify synthetic fixtures.
Do not submit real exchange orders or replace missing data with invented values.

Before changing scanner or execution behavior, read
[the financial engine](docs/FINANCIAL_ENGINE.md), [clock policy](docs/CLOCK.md) and
[accounting](docs/ACCOUNTING.md). Database corrections require new migrations;
applied migrations and historical ledger entries are immutable.

## Local verification

Use a fresh local environment as described in [development](docs/DEVELOPMENT.md).
Run checks appropriate to the change:

```bash
make test
cd frontend
npm ci
npm run typecheck
npm run build
```

`make test` recreates the disposable test database. Browser tests require the local
application and protected local test credentials; live checks are separate from
synthetic regression evidence. Do not run tests against production or start a second
Telegram consumer with the same token.

## Review checklist

- Explain the behavior, financial implications and relevant validation.
- Keep private environment files, credentials, exports, database dumps and local
  operational evidence outside Git. Do not publish secret-bearing traces or logs.
- Update the affected contract and progress notes. Report blocked checks honestly.
- Keep product claims consistent with PAPER ONLY and research limitations.
- Preserve third-party licences and attribution. This import does not select a new
  licence for owner code.

The [history disclosure](docs/HISTORY_IMPORT.md) applies to the reconstructed import.
New work should use its actual author and committer dates.
