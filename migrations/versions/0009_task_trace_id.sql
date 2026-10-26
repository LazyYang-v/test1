-- 0009: stable per-Task trace identity.
-- Shipped migrations 0001-0008 are immutable; backfill existing rows from their
-- Task UUIDs before requiring all future Tasks to provide a trace ID.

ALTER TABLE runtime.tasks
    ADD COLUMN IF NOT EXISTS trace_id TEXT;

UPDATE runtime.tasks
SET trace_id = id::text
WHERE trace_id IS NULL;

ALTER TABLE runtime.tasks
    ALTER COLUMN trace_id SET NOT NULL;

ALTER TABLE runtime.tasks
    ADD CONSTRAINT tasks_trace_id_nonempty CHECK (trace_id <> '');

CREATE UNIQUE INDEX IF NOT EXISTS tasks_trace_id_unique
    ON runtime.tasks (trace_id);

UPDATE runtime.task_events AS event
SET trace_id = task.trace_id
FROM runtime.tasks AS task
WHERE event.task_id = task.id;

UPDATE audit.audit_events AS audit
SET trace_id = task.trace_id
FROM runtime.tasks AS task
WHERE audit.resource_type = 'task'
  AND audit.resource_id = task.id::text;

UPDATE harness.model_invocations AS invocation
SET trace_id = task.trace_id
FROM runtime.tasks AS task
WHERE invocation.task_id = task.id;

UPDATE harness.tool_invocations AS invocation
SET trace_id = task.trace_id
FROM runtime.tasks AS task
WHERE invocation.task_id = task.id;

GRANT SELECT (trace_id) ON runtime.tasks TO agentforge_worker;
GRANT UPDATE (trace_id) ON harness.model_invocations TO agentforge_worker;
GRANT UPDATE (trace_id) ON harness.tool_invocations TO agentforge_worker;
