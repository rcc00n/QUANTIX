# QUANTIX infrastructure

Root [compose.yaml](../compose.yaml) is the supported local entry point: PostgreSQL
16, a one-shot Alembic migration, non-root FastAPI, a separate singleton collector,
a separate notification worker and non-root Next.js. The disposable test profile
uses separate PostgreSQL data. Runtime credentials are excluded from build contexts.

Only frontend `127.0.0.1:3100` is published locally. Backend, collector and PostgreSQL
have no published host ports. Internal collector operations require authentication.
The API labels persisted fallback data during collector outages.

Production templates supply resource limits, isolated roles, exact-host TLS routing,
capacity guards and backup timers. They require a separately authorized deployment,
verified credentials, rollback evidence, protected-dependency review and recovery
verification. Importing the templates does not certify a running installation.

Internal arbiq identifiers preserve existing imports and volume identities. No Redis,
broker, paid feed or external AI service is required.

See [development](../docs/DEVELOPMENT.md), [architecture](../docs/ARCHITECTURE.md)
and [operational boundary](../docs/DEPLOYMENT.md).
