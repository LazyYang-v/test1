-- 0004: model and tool registry, immutable agent publication, and lifecycle audit.
-- The audit sink is owned by migration 0002; this migration extends its use
-- without recreating or changing its established table definition.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentforge_registry_cli'
    ) THEN
        CREATE ROLE agentforge_registry_cli LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            PASSWORD {{env:AGENTFORGE_REGISTRY_DB_PASSWORD}};
    END IF;
END
$$;

-- Registry DDL belongs to the trusted migrator. The Registry CLI receives only
-- schema usage and table DML permissions, so it cannot disable trigger rules.
GRANT USAGE ON SCHEMA registry TO agentforge_migrator, agentforge_registry_cli,
    agentforge_api, agentforge_worker;

CREATE OR REPLACE FUNCTION registry.jsonb_has_embedded_credential(p_value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path = pg_catalog, registry
AS $$
DECLARE
    item RECORD;
BEGIN
    IF jsonb_typeof(p_value) = 'object' THEN
        FOR item IN SELECT obj.key, obj.value FROM jsonb_each(p_value) AS obj
        LOOP
            IF item.key ~* '(api[_-]?key|access[_-]?token|password|secret|credential)$'
               OR registry.jsonb_has_embedded_credential(item.value) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    ELSIF jsonb_typeof(p_value) = 'array' THEN
        FOR item IN SELECT element.value FROM jsonb_array_elements(p_value) AS element(value)
        LOOP
            IF registry.jsonb_has_embedded_credential(item.value) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    END IF;
    RETURN FALSE;
END;
$$;

CREATE TABLE IF NOT EXISTS registry.model_profiles (
    id UUID PRIMARY KEY,
    profile_key TEXT NOT NULL UNIQUE CHECK (profile_key <> ''),
    provider TEXT NOT NULL CHECK (provider <> ''),
    model_name TEXT NOT NULL CHECK (model_name <> ''),
    endpoint_alias TEXT NOT NULL CHECK (endpoint_alias <> ''),
    credential_ref TEXT NOT NULL CHECK (credential_ref <> ''),
    timeout_ms INTEGER NOT NULL CHECK (timeout_ms > 0),
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(config_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS registry.agent_versions (
    id UUID PRIMARY KEY,
    agent_key TEXT NOT NULL CHECK (agent_key <> ''),
    version TEXT NOT NULL CHECK (version <> ''),
    name TEXT NOT NULL CHECK (name <> ''),
    graph_key TEXT NOT NULL CHECK (graph_key <> ''),
    model_profile_id UUID NOT NULL REFERENCES registry.model_profiles(id),
    model_config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(model_config_snapshot) = 'object'),
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(config_json) = 'object'),
    config_hash CHAR(64) NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'retired')),
    created_by UUID,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agent_versions_key_version_unique UNIQUE (agent_key, version),
    CONSTRAINT published_agent_has_snapshot CHECK (
        status = 'draft' OR (
            model_config_snapshot <> '{}'::jsonb AND published_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS registry.tool_versions (
    id UUID PRIMARY KEY,
    tool_key TEXT NOT NULL CHECK (tool_key <> ''),
    version TEXT NOT NULL CHECK (version <> ''),
    input_schema JSONB NOT NULL CHECK (jsonb_typeof(input_schema) = 'object'),
    output_schema JSONB NOT NULL CHECK (jsonb_typeof(output_schema) = 'object'),
    policy_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(policy_json) = 'object'),
    execution_kind TEXT NOT NULL CHECK (execution_kind IN ('internal', 'mcp')),
    is_read_only BOOLEAN NOT NULL DEFAULT TRUE CHECK (is_read_only),
    timeout_ms INTEGER NOT NULL CHECK (timeout_ms > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tool_versions_key_version_unique UNIQUE (tool_key, version)
);

CREATE TABLE IF NOT EXISTS registry.agent_tool_bindings (
    agent_version_id UUID NOT NULL REFERENCES registry.agent_versions(id) ON DELETE CASCADE,
    tool_version_id UUID NOT NULL REFERENCES registry.tool_versions(id),
    policy_override JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(policy_override) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_version_id, tool_version_id)
);

CREATE OR REPLACE FUNCTION registry.policy_override_only_tightens(
    base_policy JSONB,
    policy_override JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path = pg_catalog, registry
AS $$
DECLARE
    item RECORD;
    base_value JSONB;
    override_value JSONB;
BEGIN
    IF jsonb_typeof(base_policy) <> 'object'
       OR jsonb_typeof(policy_override) <> 'object' THEN
        RETURN FALSE;
    END IF;

    FOR item IN SELECT key, value FROM jsonb_each(policy_override)
    LOOP
        base_value := base_policy -> item.key;
        override_value := item.value;

        IF left(item.key, 4) = 'max_' THEN
            IF jsonb_typeof(base_value) <> 'number'
               OR jsonb_typeof(override_value) <> 'number'
               OR (override_value #>> '{}')::numeric > (base_value #>> '{}')::numeric THEN
                RETURN FALSE;
            END IF;
        ELSIF left(item.key, 8) = 'allowed_' THEN
            IF jsonb_typeof(base_value) <> 'array'
               OR jsonb_typeof(override_value) <> 'array'
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(override_value) AS candidate(value)
                    WHERE NOT (base_value @> jsonb_build_array(candidate.value))
               ) THEN
                RETURN FALSE;
            END IF;
        ELSIF left(item.key, 8) = 'require_' THEN
            IF jsonb_typeof(base_value) <> 'boolean'
               OR jsonb_typeof(override_value) <> 'boolean'
               OR (base_value = 'false'::jsonb AND override_value <> 'true'::jsonb)
               OR (base_value = 'true'::jsonb AND override_value <> 'true'::jsonb) THEN
                RETURN FALSE;
            END IF;
        ELSIF left(item.key, 6) = 'allow_' THEN
            IF jsonb_typeof(base_value) <> 'boolean'
               OR jsonb_typeof(override_value) <> 'boolean'
               OR (base_value = 'false'::jsonb AND override_value <> 'false'::jsonb)
               OR (base_value = 'true'::jsonb AND override_value <> 'false'::jsonb
                   AND override_value <> 'true'::jsonb) THEN
                RETURN FALSE;
            END IF;
        ELSE
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION registry.guard_model_profile_credentials()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, registry
AS $$
BEGIN
    IF registry.jsonb_has_embedded_credential(NEW.config_json) THEN
        RAISE EXCEPTION 'Model profile configuration must use credential references'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION registry.guard_agent_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, registry, audit
AS $$
DECLARE
    actor_setting TEXT;
    actor_uuid UUID;
    audit_action TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'agent versions must be created as drafts'
                USING ERRCODE = '23514';
        END IF;
        IF registry.jsonb_has_embedded_credential(NEW.model_config_snapshot)
           OR registry.jsonb_has_embedded_credential(NEW.config_json) THEN
            RAISE EXCEPTION 'Agent configuration must use credential references'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'published agent versions are immutable'
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND (
        registry.jsonb_has_embedded_credential(NEW.model_config_snapshot)
        OR registry.jsonb_has_embedded_credential(NEW.config_json)
    ) THEN
        RAISE EXCEPTION 'Agent configuration must use credential references'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.status IN ('published', 'retired') THEN
        IF OLD.agent_key IS DISTINCT FROM NEW.agent_key
           OR OLD.version IS DISTINCT FROM NEW.version
           OR OLD.name IS DISTINCT FROM NEW.name
           OR OLD.graph_key IS DISTINCT FROM NEW.graph_key
           OR OLD.model_profile_id IS DISTINCT FROM NEW.model_profile_id
           OR OLD.model_config_snapshot IS DISTINCT FROM NEW.model_config_snapshot
           OR OLD.config_json IS DISTINCT FROM NEW.config_json
           OR OLD.config_hash IS DISTINCT FROM NEW.config_hash
           OR OLD.created_by IS DISTINCT FROM NEW.created_by
           OR OLD.published_at IS DISTINCT FROM NEW.published_at
           OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'published agent versions are immutable'
                USING ERRCODE = '23514';
        END IF;
        IF OLD.status = 'retired' OR NEW.status <> 'retired' THEN
            RAISE EXCEPTION 'published agent versions may only be retired'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_OP = 'UPDATE' AND OLD.status = 'draft' THEN
        IF NEW.status NOT IN ('draft', 'published') THEN
            RAISE EXCEPTION 'draft agents may only be published'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.status = 'draft' AND NEW.status = 'published' THEN
        IF NEW.model_config_snapshot = '{}'::jsonb OR NEW.published_at IS NULL THEN
            RAISE EXCEPTION 'published agent requires a frozen model configuration snapshot'
                USING ERRCODE = '23514';
        END IF;
        audit_action := 'agent.version.published';
    ELSIF TG_OP = 'UPDATE' AND OLD.status = 'published' AND NEW.status = 'retired' THEN
        audit_action := 'agent.version.retired';
    END IF;

    IF audit_action IS NOT NULL THEN
        actor_setting := current_setting('agentforge.actor_id', TRUE);
        IF actor_setting IS NOT NULL AND actor_setting <> '' THEN
            actor_uuid := actor_setting::uuid;
        END IF;
        INSERT INTO audit.audit_events
            (actor_type, actor_id, action, resource_type, resource_id, decision, metadata_json)
        VALUES (
            CASE WHEN actor_uuid IS NULL THEN 'database' ELSE 'cli' END,
            actor_uuid,
            audit_action,
            'agent_version',
            NEW.id::text,
            'allowed',
            jsonb_build_object('agent_key', NEW.agent_key, 'version', NEW.version)
        );
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION registry.guard_tool_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, registry
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'tool versions are immutable'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.tool_key IS DISTINCT FROM NEW.tool_key
       OR OLD.version IS DISTINCT FROM NEW.version
       OR OLD.input_schema IS DISTINCT FROM NEW.input_schema
       OR OLD.output_schema IS DISTINCT FROM NEW.output_schema
       OR OLD.policy_json IS DISTINCT FROM NEW.policy_json
       OR OLD.execution_kind IS DISTINCT FROM NEW.execution_kind
       OR OLD.is_read_only IS DISTINCT FROM NEW.is_read_only
       OR OLD.timeout_ms IS DISTINCT FROM NEW.timeout_ms
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'tool version configuration is immutable'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.status = 'retired' OR NEW.status NOT IN ('active', 'retired') THEN
        RAISE EXCEPTION 'retired tool versions cannot be changed'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION registry.guard_agent_tool_binding()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, registry
AS $$
DECLARE
    agent_status TEXT;
    tool_policy JSONB;
    agent_version UUID;
    tool_version UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        agent_version := OLD.agent_version_id;
        tool_version := OLD.tool_version_id;
    ELSE
        agent_version := NEW.agent_version_id;
        tool_version := NEW.tool_version_id;
    END IF;

    SELECT status INTO agent_status
    FROM registry.agent_versions WHERE id = agent_version FOR UPDATE;
    IF agent_status IN ('published', 'retired') THEN
        RAISE EXCEPTION 'published agent bindings are immutable'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP <> 'DELETE' THEN
        IF registry.jsonb_has_embedded_credential(NEW.policy_override) THEN
            RAISE EXCEPTION 'Tool policy overrides cannot contain credentials'
                USING ERRCODE = '23514';
        END IF;
        SELECT policy_json INTO tool_policy
        FROM registry.tool_versions WHERE id = tool_version;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'tool version not found' USING ERRCODE = '23503';
        END IF;
        IF NOT registry.policy_override_only_tightens(tool_policy, NEW.policy_override) THEN
            RAISE EXCEPTION 'tool policy override may only tighten the base policy'
                USING ERRCODE = '23514';
        END IF;
        IF TG_OP = 'UPDATE'
           AND (OLD.agent_version_id IS DISTINCT FROM NEW.agent_version_id
                OR OLD.tool_version_id IS DISTINCT FROM NEW.tool_version_id) THEN
            RAISE EXCEPTION 'agent tool binding keys are immutable'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS model_profiles_credentials ON registry.model_profiles;
CREATE TRIGGER model_profiles_credentials
    BEFORE INSERT OR UPDATE OF config_json ON registry.model_profiles
    FOR EACH ROW EXECUTE FUNCTION registry.guard_model_profile_credentials();

DROP TRIGGER IF EXISTS tool_versions_immutability ON registry.tool_versions;
CREATE TRIGGER tool_versions_immutability
    BEFORE UPDATE OR DELETE ON registry.tool_versions
    FOR EACH ROW EXECUTE FUNCTION registry.guard_tool_version_mutation();

DROP TRIGGER IF EXISTS agent_versions_immutability ON registry.agent_versions;
CREATE TRIGGER agent_versions_immutability
    BEFORE INSERT OR UPDATE OR DELETE ON registry.agent_versions
    FOR EACH ROW EXECUTE FUNCTION registry.guard_agent_version_mutation();

DROP TRIGGER IF EXISTS agent_tool_bindings_guard ON registry.agent_tool_bindings;
CREATE TRIGGER agent_tool_bindings_guard
    BEFORE INSERT OR UPDATE OR DELETE ON registry.agent_tool_bindings
    FOR EACH ROW EXECUTE FUNCTION registry.guard_agent_tool_binding();

GRANT SELECT, INSERT, UPDATE, DELETE ON
    registry.model_profiles,
    registry.agent_versions,
    registry.tool_versions,
    registry.agent_tool_bindings
TO agentforge_registry_cli;
GRANT SELECT ON
    registry.model_profiles,
    registry.agent_versions,
    registry.tool_versions,
    registry.agent_tool_bindings
TO agentforge_api, agentforge_worker;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA registry FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA registry FROM PUBLIC;
GRANT USAGE ON SCHEMA registry TO agentforge_migrator, agentforge_registry_cli,
    agentforge_api, agentforge_worker;
