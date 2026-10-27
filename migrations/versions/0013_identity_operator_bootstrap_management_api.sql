-- 0013: authenticated identity and Registry administration.
-- The migration is additive; it does not alter existing Region Grant semantics.

CREATE TABLE identity.operator_permissions (
    user_id UUID NOT NULL REFERENCES identity.users(id) ON DELETE RESTRICT,
    permission TEXT NOT NULL
        CHECK (permission IN ('identity_admin', 'registry_operator')),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by UUID REFERENCES identity.users(id) ON DELETE RESTRICT,
    PRIMARY KEY (user_id, permission)
);

CREATE TABLE identity.bootstrap_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    secret_digest BYTEA NOT NULL CHECK (octet_length(secret_digest) = 32),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    consumed_by UUID REFERENCES identity.users(id) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK ((consumed_at IS NULL) = (consumed_by IS NULL))
);

CREATE TABLE identity.operator_recovery_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    action TEXT NOT NULL CHECK (action IN ('disable', 'restore')),
    target_user_id UUID NOT NULL REFERENCES identity.users(id) ON DELETE RESTRICT,
    secret_digest BYTEA NOT NULL CHECK (octet_length(secret_digest) = 32),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    consumed_by UUID REFERENCES identity.users(id) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK ((consumed_at IS NULL) = (consumed_by IS NULL))
);

CREATE FUNCTION identity.prevent_bootstrap_secret_reuse()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, identity
AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL AND (
        NEW.secret_digest IS DISTINCT FROM OLD.secret_digest
        OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
        OR NEW.consumed_by IS DISTINCT FROM OLD.consumed_by
    ) THEN
        RAISE EXCEPTION 'consumed bootstrap state cannot be changed'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.consumed_at IS NULL AND NEW.consumed_at IS NOT NULL AND (
        NEW.secret_digest IS DISTINCT FROM OLD.secret_digest
        OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    ) THEN
        RAISE EXCEPTION 'bootstrap consumption cannot rotate its secret'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER bootstrap_state_no_reuse
    BEFORE UPDATE ON identity.bootstrap_state
    FOR EACH ROW EXECUTE FUNCTION identity.prevent_bootstrap_secret_reuse();

CREATE FUNCTION identity.prevent_operator_recovery_reuse()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, identity
AS $$
BEGIN
    -- The trusted migrator may rotate the scoped break-glass capability;
    -- the API role can only consume it and cannot reset a consumed secret.
    IF current_user = 'agentforge_migrator'
       AND NEW.consumed_at IS NULL AND NEW.consumed_by IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.consumed_at IS NOT NULL AND (
        NEW.action IS DISTINCT FROM OLD.action
        OR NEW.target_user_id IS DISTINCT FROM OLD.target_user_id
        OR NEW.secret_digest IS DISTINCT FROM OLD.secret_digest
        OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
        OR NEW.consumed_by IS DISTINCT FROM OLD.consumed_by
    ) THEN
        RAISE EXCEPTION 'consumed operator recovery state cannot be changed'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.consumed_at IS NULL AND NEW.consumed_at IS NOT NULL AND (
        NEW.action IS DISTINCT FROM OLD.action
        OR NEW.target_user_id IS DISTINCT FROM OLD.target_user_id
        OR NEW.secret_digest IS DISTINCT FROM OLD.secret_digest
        OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    ) THEN
        RAISE EXCEPTION 'operator recovery consumption cannot change its scope'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER operator_recovery_state_no_reuse
    BEFORE UPDATE ON identity.operator_recovery_state
    FOR EACH ROW EXECUTE FUNCTION identity.prevent_operator_recovery_reuse();

REVOKE ALL PRIVILEGES ON
    identity.operator_permissions,
    identity.bootstrap_state,
    identity.operator_recovery_state
FROM PUBLIC;

GRANT SELECT, INSERT ON identity.operator_permissions TO agentforge_api;
GRANT SELECT ON identity.bootstrap_state,
    identity.operator_recovery_state TO agentforge_api;
GRANT UPDATE (consumed_at, consumed_by) ON identity.bootstrap_state,
    identity.operator_recovery_state TO agentforge_api;
GRANT INSERT (id, email, password_hash, display_name, status, token_version)
    ON identity.users TO agentforge_api;
GRANT UPDATE (password_hash, status, token_version, updated_at)
    ON identity.users TO agentforge_api;
GRANT INSERT (id, user_id, region_code, permission)
    ON identity.region_grants TO agentforge_api;

REVOKE INSERT, UPDATE, DELETE ON
    registry.model_profiles,
    registry.agent_versions,
    registry.tool_versions,
    registry.agent_tool_bindings
FROM agentforge_registry_cli;

GRANT INSERT (id, profile_key, provider, model_name, endpoint_alias, credential_ref,
              timeout_ms, max_output_tokens, config_json)
    ON registry.model_profiles TO agentforge_api;
GRANT INSERT (id, agent_key, version, name, graph_key, model_profile_id,
              model_config_snapshot, config_json, config_hash, status, created_by)
    ON registry.agent_versions TO agentforge_api;
GRANT UPDATE (model_config_snapshot, config_hash, status, published_at)
    ON registry.agent_versions TO agentforge_api;
GRANT INSERT (id, tool_key, version, input_schema, output_schema, policy_json,
              execution_kind, timeout_ms)
    ON registry.tool_versions TO agentforge_api;
GRANT INSERT (agent_version_id, tool_version_id, policy_override)
    ON registry.agent_tool_bindings TO agentforge_api;

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
            CASE WHEN actor_uuid IS NULL THEN 'database' ELSE 'user' END,
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
