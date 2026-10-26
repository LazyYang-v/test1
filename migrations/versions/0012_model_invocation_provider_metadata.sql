-- Additive per-call Provider metadata for reproducible model invocation records.
ALTER TABLE harness.model_invocations
    ADD COLUMN IF NOT EXISTS provider_request_id TEXT,
    ADD COLUMN IF NOT EXISTS provider_parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER NOT NULL DEFAULT 0
        CHECK (duration_ms >= 0);

GRANT UPDATE (provider_request_id, provider_parameters, duration_ms)
    ON harness.model_invocations TO agentforge_worker;
