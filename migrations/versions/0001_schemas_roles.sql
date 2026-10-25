-- 0001: database schemas, platform roles, and schema-level grants
-- (P0 design baseline §3.1/§3.7)
--
-- Idempotent: every statement is safe to re-execute, so re-running this
-- migration (e.g. after clearing schema_migrations bookkeeping) succeeds.
--
-- No extensions are required: primary keys are app-side UUIDv7 and hashing
-- is app-side, both per the design baseline field conventions.
--
-- agentforge_migrator normally pre-exists (bootstrapped by the runner's
-- bootstrap step, which needs cluster privileges once); its guard block is
-- kept here so this file is the single source of truth for all five roles.
-- Passwords come from the environment via {{env:NAME}} tokens substituted
-- by the runner at apply time; they are never stored in this repository.

CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS registry;
CREATE SCHEMA IF NOT EXISTS runtime;
CREATE SCHEMA IF NOT EXISTS harness;
CREATE SCHEMA IF NOT EXISTS business;
CREATE SCHEMA IF NOT EXISTS evaluation;
CREATE SCHEMA IF NOT EXISTS audit;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentforge_migrator') THEN
        CREATE ROLE agentforge_migrator LOGIN NOSUPERUSER NOCREATEDB CREATEROLE PASSWORD {{env:AGENTFORGE_MIGRATOR_DB_PASSWORD}};
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentforge_api') THEN
        CREATE ROLE agentforge_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {{env:AGENTFORGE_API_DB_PASSWORD}};
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentforge_worker') THEN
        CREATE ROLE agentforge_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {{env:AGENTFORGE_WORKER_DB_PASSWORD}};
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentforge_business_ro') THEN
        CREATE ROLE agentforge_business_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {{env:AGENTFORGE_BUSINESS_RO_DB_PASSWORD}};
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentforge_eval') THEN
        CREATE ROLE agentforge_eval LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {{env:AGENTFORGE_EVAL_DB_PASSWORD}};
    END IF;
END
$$;

-- Schema usage per role (table-level grants come with the migrations that
-- create the tables, 0002+). agentforge.schema_migrations exists already:
-- the runner ensures it before applying this migration.
GRANT USAGE ON SCHEMA agentforge TO agentforge_api, agentforge_worker;
GRANT SELECT ON agentforge.schema_migrations TO agentforge_api, agentforge_worker;
GRANT USAGE ON SCHEMA identity, registry, runtime, harness, audit TO agentforge_api;
GRANT USAGE ON SCHEMA runtime, harness, audit TO agentforge_worker;
GRANT USAGE ON SCHEMA business TO agentforge_business_ro;
GRANT USAGE ON SCHEMA evaluation TO agentforge_eval;
