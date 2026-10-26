-- 0008: attempts, step executions, harness invocations, platform evidence, and
-- the least-privilege grants the worker and API need for the tracer bullet.
--
-- Shipped migrations 0001-0007 are immutable; this file only adds objects.

CREATE TABLE IF NOT EXISTS runtime.task_attempts (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES runtime.tasks(id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'abandoned')),
    worker_id TEXT NOT NULL CHECK (worker_id <> ''),
    lease_token UUID NOT NULL,
    fencing_epoch BIGINT NOT NULL CHECK (fencing_epoch > 0),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checkpoint_ref TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    error_code TEXT,
    error_detail TEXT,
    CONSTRAINT task_attempts_task_attempt_unique UNIQUE (task_id, attempt_no),
    CONSTRAINT task_attempts_task_epoch_unique UNIQUE (task_id, fencing_epoch)
);

CREATE INDEX IF NOT EXISTS task_attempts_task_status_idx
    ON runtime.task_attempts (task_id, status);
CREATE INDEX IF NOT EXISTS task_attempts_live_lease_idx
    ON runtime.task_attempts (lease_expires_at) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS runtime.step_executions (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES runtime.tasks(id),
    step_key VARCHAR(160) NOT NULL CHECK (step_key <> ''),
    graph_node_key TEXT NOT NULL CHECK (graph_node_key <> ''),
    input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'failed')),
    result_ref TEXT,
    result_json JSONB
        CHECK (result_json IS NULL OR jsonb_typeof(result_json) = 'object'),
    result_hash CHAR(64)
        CHECK (result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
    checkpoint_ref TEXT,
    owning_attempt_id UUID REFERENCES runtime.task_attempts(id),
    fencing_epoch BIGINT CHECK (fencing_epoch IS NULL OR fencing_epoch > 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT step_executions_task_step_unique UNIQUE (task_id, step_key)
);

CREATE INDEX IF NOT EXISTS step_executions_task_status_idx
    ON runtime.step_executions (task_id, status);

-- A confirmed step is the replay-memoization source of truth. Later attempts
-- may reference it, never overwrite it.
CREATE OR REPLACE FUNCTION runtime.guard_confirmed_step_execution()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'confirmed' THEN
        RAISE EXCEPTION 'confirmed step executions are immutable'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
REVOKE EXECUTE ON FUNCTION runtime.guard_confirmed_step_execution() FROM PUBLIC;

DROP TRIGGER IF EXISTS step_executions_confirmed_immutable ON runtime.step_executions;
CREATE TRIGGER step_executions_confirmed_immutable
    BEFORE UPDATE OR DELETE ON runtime.step_executions
    FOR EACH ROW EXECUTE FUNCTION runtime.guard_confirmed_step_execution();

CREATE TABLE IF NOT EXISTS harness.model_invocations (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES runtime.tasks(id),
    attempt_id UUID NOT NULL REFERENCES runtime.task_attempts(id),
    invocation_key TEXT NOT NULL CHECK (invocation_key <> ''),
    agent_version_id UUID NOT NULL REFERENCES registry.agent_versions(id),
    provider TEXT NOT NULL CHECK (provider <> ''),
    model_name TEXT NOT NULL CHECK (model_name <> ''),
    context_hash CHAR(64) NOT NULL CHECK (context_hash ~ '^[0-9a-f]{64}$'),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    estimated_cost NUMERIC(18, 8) NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
    status TEXT NOT NULL CHECK (status IN ('executing', 'succeeded', 'failed')),
    error_code TEXT,
    trace_id TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT model_invocations_task_key_unique UNIQUE (task_id, invocation_key)
);

CREATE INDEX IF NOT EXISTS model_invocations_task_idx
    ON harness.model_invocations (task_id, started_at);

CREATE TABLE IF NOT EXISTS harness.tool_invocations (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES runtime.tasks(id),
    attempt_id UUID NOT NULL REFERENCES runtime.task_attempts(id),
    invocation_key TEXT NOT NULL CHECK (invocation_key <> ''),
    tool_version_id UUID NOT NULL REFERENCES registry.tool_versions(id),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    request_json JSONB NOT NULL CHECK (jsonb_typeof(request_json) = 'object'),
    status TEXT NOT NULL
        CHECK (status IN ('prepared', 'executing', 'succeeded', 'failed', 'unknown')),
    result_json JSONB
        CHECK (result_json IS NULL OR jsonb_typeof(result_json) = 'object'),
    result_hash CHAR(64)
        CHECK (result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
    error_code TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    trace_id TEXT,
    CONSTRAINT tool_invocations_task_key_unique UNIQUE (task_id, invocation_key)
);

CREATE INDEX IF NOT EXISTS tool_invocations_task_idx
    ON harness.tool_invocations (task_id, started_at);

CREATE TABLE IF NOT EXISTS harness.evidences (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES runtime.tasks(id),
    attempt_id UUID REFERENCES runtime.task_attempts(id),
    tool_invocation_id UUID REFERENCES harness.tool_invocations(id),
    metric_key TEXT NOT NULL CHECK (metric_key <> ''),
    metric_version INTEGER NOT NULL CHECK (metric_version > 0),
    unit TEXT NOT NULL CHECK (unit <> ''),
    current_value NUMERIC(18, 2) NOT NULL,
    previous_value NUMERIC(18, 2) NOT NULL,
    absolute_change NUMERIC(18, 2) NOT NULL,
    change_rate NUMERIC(18, 8),
    query_plan_json JSONB NOT NULL CHECK (jsonb_typeof(query_plan_json) = 'object'),
    result_json JSONB NOT NULL CHECK (jsonb_typeof(result_json) = 'object'),
    result_set_hash CHAR(64) NOT NULL CHECK (result_set_hash ~ '^[0-9a-f]{64}$'),
    row_count BIGINT NOT NULL CHECK (row_count >= 0),
    data_snapshot_id CHAR(64) NOT NULL
        REFERENCES business.data_snapshots(data_snapshot_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS evidences_task_idx
    ON harness.evidences (task_id, metric_key, metric_version);
CREATE UNIQUE INDEX IF NOT EXISTS evidences_tool_invocation_unique
    ON harness.evidences (tool_invocation_id)
    WHERE tool_invocation_id IS NOT NULL;

CREATE OR REPLACE FUNCTION harness.reject_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'evidence records are immutable'
        USING ERRCODE = 'P0001';
END;
$$;
REVOKE EXECUTE ON FUNCTION harness.reject_evidence_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS evidences_immutable ON harness.evidences;
CREATE TRIGGER evidences_immutable
    BEFORE UPDATE OR DELETE ON harness.evidences
    FOR EACH ROW EXECUTE FUNCTION harness.reject_evidence_mutation();

-- Attach the deferred attempt foreign key now that attempts exist.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'task_events_attempt_fk'
          AND conrelid = 'runtime.task_events'::regclass
    ) THEN
        ALTER TABLE runtime.task_events
            ADD CONSTRAINT task_events_attempt_fk
            FOREIGN KEY (attempt_id) REFERENCES runtime.task_attempts(id);
    END IF;
END
$$;

-- The deterministic P0 metrics tool. Ticket 07 replaces the executor behind the
-- same registered, read-only tool contract; the input/output schemas are fixed
-- here so the gateway can validate both sides.
INSERT INTO registry.tool_versions (
    id, tool_key, version, input_schema, output_schema, policy_json,
    execution_kind, is_read_only, timeout_ms, status
)
VALUES (
    '00000000-0000-7000-8000-000000000601',
    'metrics.query',
    '1',
    '{
      "type": "object",
      "additionalProperties": false,
      "required": ["metric", "metric_version", "dimensions", "filters",
                   "time_range", "comparison", "limit"],
      "properties": {
        "metric": {"type": "string", "enum": ["gmv"]},
        "metric_version": {"type": "integer", "const": 1},
        "dimensions": {"type": "array", "items": {"type": "string"},
                       "maxItems": 4},
        "filters": {
          "type": "object",
          "additionalProperties": false,
          "required": ["region_code"],
          "properties": {
            "region_code": {"type": "array", "minItems": 1,
                            "items": {"type": "string"}}
          }
        },
        "time_range": {
          "type": "object",
          "additionalProperties": false,
          "required": ["start", "end_exclusive", "timezone"],
          "properties": {
            "start": {"type": "string"},
            "end_exclusive": {"type": "string"},
            "timezone": {"type": "string"}
          }
        },
        "comparison": {"type": "string", "enum": ["previous_period"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}
      }
    }'::jsonb,
    '{
      "type": "object",
      "additionalProperties": false,
      "required": ["metric", "metric_version", "unit", "current_value",
                   "previous_value", "absolute_change", "change_rate",
                   "row_count", "data_snapshot_id"],
      "properties": {
        "metric": {"type": "string"},
        "metric_version": {"type": "integer"},
        "unit": {"type": "string"},
        "current_value": {"type": "string"},
        "previous_value": {"type": "string"},
        "absolute_change": {"type": "string"},
        "change_rate": {"type": ["string", "null"]},
        "row_count": {"type": "integer", "minimum": 0},
        "data_snapshot_id": {"type": "string"}
      }
    }'::jsonb,
    '{"max_calls": 8, "allowed_metrics": ["gmv"], "require_region_scope": true}'::jsonb,
    'internal',
    TRUE,
    15000,
    'active'
)
ON CONFLICT (tool_key, version) DO NOTHING;

REVOKE ALL PRIVILEGES ON
    runtime.task_attempts,
    runtime.step_executions,
    harness.model_invocations,
    harness.tool_invocations,
    harness.evidences
FROM PUBLIC;

-- Worker: lifecycle writes only on rows it owns, plus append-only events.
GRANT SELECT ON runtime.tasks, runtime.task_events TO agentforge_worker;
GRANT UPDATE (status, current_attempt_no, result_json, error_code,
              error_message, completed_at, next_retry_at, cancel_requested_at,
              updated_at, version)
    ON runtime.tasks TO agentforge_worker;
GRANT SELECT, INSERT ON runtime.task_events TO agentforge_worker;
GRANT USAGE, SELECT ON SEQUENCE runtime.task_events_id_seq TO agentforge_worker;

GRANT SELECT, INSERT ON runtime.task_attempts TO agentforge_worker;
GRANT UPDATE (status, lease_token, fencing_epoch, lease_expires_at,
              heartbeat_at, checkpoint_ref, finished_at, error_code,
              error_detail)
    ON runtime.task_attempts TO agentforge_worker;

GRANT SELECT, INSERT ON runtime.step_executions TO agentforge_worker;
GRANT UPDATE (status, result_ref, result_json, result_hash, checkpoint_ref,
              owning_attempt_id, fencing_epoch, completed_at)
    ON runtime.step_executions TO agentforge_worker;

GRANT SELECT, INSERT ON harness.model_invocations TO agentforge_worker;
GRANT UPDATE (status, input_tokens, output_tokens, estimated_cost,
              error_code, finished_at)
    ON harness.model_invocations TO agentforge_worker;

GRANT SELECT, INSERT ON harness.tool_invocations TO agentforge_worker;
GRANT UPDATE (status, result_json, result_hash, error_code, finished_at)
    ON harness.tool_invocations TO agentforge_worker;

GRANT SELECT, INSERT ON harness.evidences TO agentforge_worker;

-- API: read-only visibility for task detail/progress and admin views.
GRANT SELECT ON
    runtime.task_attempts,
    runtime.step_executions,
    harness.model_invocations,
    harness.tool_invocations,
    harness.evidences
TO agentforge_api;
