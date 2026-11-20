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

