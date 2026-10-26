-- 0006: durable task creation, lifecycle events, and idempotency records.

CREATE TABLE IF NOT EXISTS runtime.tasks (
    id UUID PRIMARY KEY,
    owner_user_id UUID NOT NULL REFERENCES identity.users(id),
    agent_version_id UUID NOT NULL REFERENCES registry.agent_versions(id),
    task_type TEXT NOT NULL CHECK (task_type <> ''),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'retry_wait', 'recovering',
                          'cancelling', 'completed', 'failed', 'cancelled')),
    input_json JSONB NOT NULL CHECK (jsonb_typeof(input_json) = 'object'),
    input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    region_scope_json JSONB NOT NULL
        CHECK (jsonb_typeof(region_scope_json) = 'array'),
    authorization_snapshot JSONB NOT NULL
        CHECK (jsonb_typeof(authorization_snapshot) = 'array'),
    data_snapshot_id CHAR(64) NOT NULL
        REFERENCES business.data_snapshots(data_snapshot_id),
    priority SMALLINT NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    current_attempt_no INTEGER NOT NULL DEFAULT 0 CHECK (current_attempt_no >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 3),
    deadline_at TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result_json JSONB,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0)
);

CREATE INDEX IF NOT EXISTS tasks_status_priority_created_idx
    ON runtime.tasks (status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS tasks_owner_created_idx
    ON runtime.tasks (owner_user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS tasks_agent_created_idx
    ON runtime.tasks (agent_version_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_next_retry_idx
    ON runtime.tasks (next_retry_at) WHERE status = 'retry_wait';

CREATE TABLE IF NOT EXISTS runtime.task_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES runtime.tasks(id),
    attempt_id UUID,
    task_event_seq BIGINT NOT NULL CHECK (task_event_seq > 0),
    event_type TEXT NOT NULL CHECK (event_type <> ''),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload_json) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id TEXT,
    CONSTRAINT task_events_task_seq_unique UNIQUE (task_id, task_event_seq)
);
CREATE INDEX IF NOT EXISTS task_events_task_seq_idx
    ON runtime.task_events (task_id, task_event_seq);

CREATE OR REPLACE FUNCTION runtime.reject_task_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'task events are append-only' USING ERRCODE = 'P0001';
END;
$$;
DROP TRIGGER IF EXISTS task_events_append_only ON runtime.task_events;
CREATE TRIGGER task_events_append_only
    BEFORE UPDATE OR DELETE ON runtime.task_events
    FOR EACH ROW EXECUTE FUNCTION runtime.reject_task_event_mutation();

CREATE TABLE IF NOT EXISTS runtime.idempotency_records (
    id UUID PRIMARY KEY,
    actor_user_id UUID NOT NULL REFERENCES identity.users(id),
    operation TEXT NOT NULL CHECK (operation <> ''),
    idempotency_key TEXT NOT NULL
        CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
    resource_id UUID REFERENCES runtime.tasks(id),
    response_status SMALLINT,
    response_json JSONB,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT idempotency_records_actor_operation_key_unique
        UNIQUE (actor_user_id, operation, idempotency_key),
    CONSTRAINT idempotency_records_response_matches_status CHECK (
        (status = 'processing' AND response_status IS NULL AND response_json IS NULL)
        OR (status = 'completed' AND response_status = 202 AND response_json IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idempotency_records_expiry_idx
    ON runtime.idempotency_records (expires_at);

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA runtime FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION runtime.reject_task_event_mutation() FROM PUBLIC;
GRANT USAGE ON SCHEMA runtime TO agentforge_api;
GRANT SELECT, INSERT ON runtime.tasks, runtime.task_events,
    runtime.idempotency_records TO agentforge_api;
GRANT UPDATE ON runtime.idempotency_records TO agentforge_api;
GRANT USAGE, SELECT ON SEQUENCE runtime.task_events_id_seq TO agentforge_api;

-- Task creation can read frozen snapshot metadata but never raw business rows.
GRANT USAGE ON SCHEMA business TO agentforge_api;
GRANT SELECT ON business.data_snapshots, business.dataset_loads TO agentforge_api;
