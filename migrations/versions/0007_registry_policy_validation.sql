-- 0007: fail closed when Registry policy overrides have no typed base value.

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
    IF jsonb_typeof(base_policy) IS DISTINCT FROM 'object'
       OR jsonb_typeof(policy_override) IS DISTINCT FROM 'object' THEN
        RETURN FALSE;
    END IF;

    FOR item IN SELECT key, value FROM jsonb_each(policy_override)
    LOOP
        IF NOT (base_policy ? item.key) THEN
            RETURN FALSE;
        END IF;
        base_value := base_policy -> item.key;
        override_value := item.value;

        IF left(item.key, 4) = 'max_' THEN
            IF jsonb_typeof(base_value) IS DISTINCT FROM 'number'
               OR jsonb_typeof(override_value) IS DISTINCT FROM 'number' THEN
                RETURN FALSE;
            END IF;
            IF (override_value #>> '{}')::numeric > (base_value #>> '{}')::numeric THEN
                RETURN FALSE;
            END IF;
        ELSIF left(item.key, 8) = 'allowed_' THEN
            IF jsonb_typeof(base_value) IS DISTINCT FROM 'array'
               OR jsonb_typeof(override_value) IS DISTINCT FROM 'array' THEN
                RETURN FALSE;
            END IF;
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(override_value) AS candidate(value)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(base_value) AS permitted(value)
                    WHERE permitted.value = candidate.value
                )
            ) THEN
                RETURN FALSE;
            END IF;
        ELSIF left(item.key, 8) = 'require_' THEN
            IF jsonb_typeof(base_value) IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(override_value) IS DISTINCT FROM 'boolean'
               OR override_value <> 'true'::jsonb THEN
                RETURN FALSE;
            END IF;
        ELSIF left(item.key, 6) = 'allow_' THEN
            IF jsonb_typeof(base_value) IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(override_value) IS DISTINCT FROM 'boolean'
               OR (base_value = 'false'::jsonb AND override_value <> 'false'::jsonb) THEN
                RETURN FALSE;
            END IF;
        ELSE
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
END;
$$;

-- Never preserve a binding accepted by the old validator without checking it,
-- and never rewrite an already published configuration to make it pass.
LOCK TABLE registry.agent_tool_bindings IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE
    invalid_binding RECORD;
BEGIN
    SELECT binding.agent_version_id, binding.tool_version_id, agent.status
    INTO invalid_binding
    FROM registry.agent_tool_bindings AS binding
    JOIN registry.agent_versions AS agent ON agent.id = binding.agent_version_id
    JOIN registry.tool_versions AS tool ON tool.id = binding.tool_version_id
    WHERE registry.policy_override_only_tightens(tool.policy_json, binding.policy_override) IS NOT TRUE
    ORDER BY binding.agent_version_id, binding.tool_version_id
    LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'existing invalid Registry policy override: agent_version_id=%, tool_version_id=%, status=%',
            invalid_binding.agent_version_id, invalid_binding.tool_version_id, invalid_binding.status
            USING ERRCODE = '23514',
                HINT = 'Reconcile the invalid binding before retrying; published configurations must not be rewritten.';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION registry.guard_agent_publication_policy()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, registry
AS $$
BEGIN
    IF OLD.status = 'draft' AND NEW.status = 'published' AND EXISTS (
        SELECT 1
        FROM registry.agent_tool_bindings AS binding
        JOIN registry.tool_versions AS tool ON tool.id = binding.tool_version_id
        WHERE binding.agent_version_id = NEW.id
          AND registry.policy_override_only_tightens(tool.policy_json, binding.policy_override) IS NOT TRUE
    ) THEN
        RAISE EXCEPTION 'tool policy override may only tighten the base policy'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS agent_versions_publication_policy ON registry.agent_versions;
CREATE TRIGGER agent_versions_publication_policy
    BEFORE UPDATE OF status ON registry.agent_versions
    FOR EACH ROW EXECUTE FUNCTION registry.guard_agent_publication_policy();

CREATE OR REPLACE FUNCTION registry.guard_version_identifier()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, registry
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION 'Registry version identifiers are immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS agent_versions_identifier ON registry.agent_versions;
CREATE TRIGGER agent_versions_identifier
    BEFORE UPDATE OF id ON registry.agent_versions
    FOR EACH ROW EXECUTE FUNCTION registry.guard_version_identifier();

DROP TRIGGER IF EXISTS tool_versions_identifier ON registry.tool_versions;
CREATE TRIGGER tool_versions_identifier
    BEFORE UPDATE OF id ON registry.tool_versions
    FOR EACH ROW EXECUTE FUNCTION registry.guard_version_identifier();

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA registry FROM PUBLIC,
    agentforge_registry_cli, agentforge_api, agentforge_worker,
    agentforge_business_ro, agentforge_eval;
