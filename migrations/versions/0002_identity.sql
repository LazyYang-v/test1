-- 0002: identity records, live region grants, and auth audit persistence
-- (P0 design baseline §§3.1, 3.6, 9)
--
-- The audit sink is created here because identity login and live region checks
-- must emit durable security decisions from the first schema that supports them.

CREATE TABLE IF NOT EXISTS identity.users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    token_version INTEGER NOT NULL DEFAULT 1 CHECK (token_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_email_normalized CHECK (email = lower(btrim(email))),
    CONSTRAINT users_email_nonempty CHECK (email <> '')
);

CREATE TABLE IF NOT EXISTS identity.region_grants (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES identity.users(id),
    region_code TEXT NOT NULL CHECK (region_code <> ''),
    permission TEXT NOT NULL CHECK (permission IN ('read', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS region_grants_one_active_permission
    ON identity.region_grants (user_id, region_code, permission)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS region_grants_active_by_user
    ON identity.region_grants (user_id, region_code)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS audit.audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id UUID,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('allowed', 'denied')),
    reason_code TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    trace_id TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA identity TO agentforge_api, agentforge_worker;
GRANT SELECT ON identity.users, identity.region_grants TO agentforge_api;
GRANT SELECT ON identity.region_grants TO agentforge_worker;

GRANT INSERT ON audit.audit_events TO agentforge_api, agentforge_worker;
GRANT USAGE, SELECT ON SEQUENCE audit.audit_events_id_seq TO agentforge_api, agentforge_worker;
