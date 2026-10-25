#!/usr/bin/env bash
set -eu

psql --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname postgres \
    --set migrator_password="$AGENTFORGE_MIGRATOR_DB_PASSWORD" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB CREATEROLE PASSWORD %L',
    'agentforge_migrator',
    :'migrator_password'
)
WHERE NOT EXISTS (
    SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentforge_migrator'
)
\gexec
ALTER DATABASE agentforge OWNER TO agentforge_migrator;
SQL
