# QUANTIX operational boundary

The repository includes QUANTIX-owned production templates and safety tooling. A
source import does not authorize deployment, server changes, proxy reloads or
computer shutdown. Owner-specific host inventories, release identifiers, portfolio
state and recovery proofs remain in the private source backup.

## Isolation and routine inspection

The local stack is documented in [DEVELOPMENT.md](DEVELOPMENT.md) and
[infra/README.md](../infra/README.md). Its only published service is the frontend
on loopback. Public operation requires authentication and TLS, a private database,
least-privilege credentials, resource limits and independent capacity verification.
Credentials are file-backed; private environment files and `.secrets` are excluded
from Git and build contexts. Runtime database authentication must be tested through
actual TCP authentication, separately from administrative SQL privileges.

Review the production Compose configuration without starting it:

```bash
make production-config
```

This validates the checked-in production templates. It is not a deployment or a
capacity, recovery or security acceptance result for a running host.

## Resource safety and stopping

The production templates cap memory, swap, CPU, logs and process counts. Collector
queues, persistence batches, caches and exports are bounded independently. A cold
cache or restart is not sustained optimization evidence. Financial evidence is
never pruned to meet a capacity target. Retention is limited to explicitly transient,
unreferenced market samples. Keep rollback evidence and protect unrelated services.

Notification delivery has its own singleton worker. Never start a second consumer
for the same Telegram token. Collector and notification ownership, durable offsets,
ambiguous send outcomes and clock uncertainty must survive release transitions.

## Backups, restore and disk runway

`scripts/backup_database.py` creates private PostgreSQL custom-format archives with
checksummed metadata, atomic publication and a directory lock. Retention recognizes
verified archive/metadata pairs; an arbitrary file is not a proven backup.
`scripts/verify_restore.py` restores to disposable isolated resources, checks schema
and accounting, and can require PAPER ONLY. A copied file, matching hash and successful
restore are distinct evidence. Recurring offsite recovery and unattended disk runway
require separate measured verification.

Inspect available command arguments before planning an operation:

```bash
python3 scripts/backup_database.py --help
python3 scripts/verify_restore.py --help
python3 scripts/retention_runway.py --help
```

Private database archives, financial exports and restore proofs never belong in the
repository. No backup or restoration was performed by the reconstructed import.

## TLS and protected NGINX

The templates use an exact-host vhost and isolated certificate configuration,
work and logs. `scripts/guarded_nginx_reload.py` and
`scripts/renew_quantix_tls.py` enforce protected-configuration boundaries. A strict
unit must not gain write access to unrelated host logs or shared PID files. Never
refresh a protected baseline blindly after another application's change. Validate
full configuration and rollback before an explicitly authorized additive route.

## Production smoke and local cutover boundary

A running release needs independent evidence for authenticated HTTPS, exact schema,
internal-service isolation, trusted clock, collector health, accounting invariants,
export integrity, recovery and protected dependencies. New paper portfolios require
explicit funding; starting services never establishes profitable execution.
Production ownership of Telegram requires the local consumer to stay disabled.
See [notifications](NOTIFICATIONS.md), [accounting](ACCOUNTING.md),
[reporting](REPORTING.md) and [history disclosure](HISTORY_IMPORT.md).

