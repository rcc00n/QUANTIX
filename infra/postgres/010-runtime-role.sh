#!/bin/sh
# Runs only in QUANTIX's new isolated PostgreSQL volume. Never prints credentials.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\set VERBOSITY terse
DO $role$
BEGIN
  EXECUTE format(
    'CREATE ROLE quantix_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    btrim(pg_read_file('/run/secrets/pg_runtime_password'), E' \t\r\n')
  );
END
$role$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE quantix TO quantix_runtime;
GRANT USAGE ON SCHEMA public TO quantix_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE quantix_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quantix_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE quantix_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO quantix_runtime;
SQL
