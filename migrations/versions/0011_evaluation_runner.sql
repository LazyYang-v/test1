-- 0011: evaluation runner tables and the service identity that owns their
-- runtime Tasks. Dataset fixtures are seeded by the migration so the
-- benchmark is repeatable in every environment.

CREATE TABLE IF NOT EXISTS evaluation.datasets (
    id UUID PRIMARY KEY,
    dataset_key VARCHAR(100) NOT NULL,
    version INTEGER NOT NULL,
    name VARCHAR(160) NOT NULL,
    description TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    data_snapshot_id CHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'retired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_key, version)
);

CREATE TABLE IF NOT EXISTS evaluation.cases (
    id UUID PRIMARY KEY,
    dataset_id UUID NOT NULL REFERENCES evaluation.datasets (id),
    case_key VARCHAR(100) NOT NULL,
    input_json JSONB NOT NULL,
    expected_json JSONB NOT NULL,
    assertions_json JSONB NOT NULL,
    data_snapshot_ref CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, case_key)
);

CREATE TABLE IF NOT EXISTS evaluation.runs (
    id UUID PRIMARY KEY,
    dataset_id UUID NOT NULL REFERENCES evaluation.datasets (id),
    agent_version_id UUID NOT NULL REFERENCES registry.agent_versions (id),
    status VARCHAR(30) NOT NULL
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    model_mode VARCHAR(10) NOT NULL DEFAULT 'fake'
        CHECK (model_mode IN ('fake', 'live')),
    live_call_budget INTEGER
        CHECK (live_call_budget IS NULL OR live_call_budget > 0),
    live_calls_used INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    summary_json JSONB,
    CHECK (model_mode = 'fake' OR live_call_budget IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS evaluation.case_results (
    id UUID PRIMARY KEY,
    eval_run_id UUID NOT NULL REFERENCES evaluation.runs (id),
    case_id UUID NOT NULL REFERENCES evaluation.cases (id),
    task_id UUID REFERENCES runtime.tasks (id),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'passed', 'failed')),
    score_json JSONB NOT NULL DEFAULT '{}',
    failure_codes TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (eval_run_id, case_id)
);

CREATE INDEX IF NOT EXISTS evaluation_runs_dataset_idx
    ON evaluation.runs (dataset_id, started_at DESC);

-- Evaluation Tasks link back to their run without altering the public task
-- contract; the owner column carries the service identity.
ALTER TABLE runtime.tasks
    ADD COLUMN IF NOT EXISTS eval_run_id UUID REFERENCES evaluation.runs (id);

-- The service account that owns every evaluation Task. It is not a login
-- principal: tokens are minted internally by the evaluation runner only.
INSERT INTO identity.users (id, email, display_name, status)
VALUES (
    '00000000-0000-7000-8000-000000000e0a1'::uuid,
    'svc-eval@agentforge.internal',
    'Evaluation Service',
    'active'
)
ON CONFLICT (email) DO NOTHING;

-- Fixture region for the benchmark tasks, granted read-only to the service.
INSERT INTO identity.region_grants (id, user_id, region_code, permission)
SELECT '00000000-0000-7000-8000-000000000e0a2'::uuid,
       service.id, 'CN_EAST', 'read'
FROM identity.users AS service
WHERE service.email = 'svc-eval@agentforge.internal'
  AND NOT EXISTS (
      SELECT 1 FROM identity.region_grants AS existing
      WHERE existing.user_id = service.id AND existing.region_code = 'CN_EAST'
        AND existing.permission = 'read' AND existing.revoked_at IS NULL
  );

-- The frozen benchmark dataset bound to the canonical data snapshot. Cases
-- mirror agentforge_core.evaluation.CORE_CASES exactly; the canonical JSON
-- payloads keep the benchmark deterministic in every environment.
INSERT INTO evaluation.datasets (
    id, dataset_key, version, name, description, content_hash,
    data_snapshot_id, status
)
VALUES (
    '00000000-0000-7000-8000-000000000e0b1'::uuid,
    'p0-core-business-analysis',
    1,
    'P0 core business analysis benchmark',
    'Frozen ten-case GMV benchmark over the canonical dataset snapshot.',
    'c269d0e1a5ff2f3ed745e5f3b6e88ba4e2ef6d0f70a50c0e6a3f7d2b7a5c9f4e'::text,
    'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8',
    'active'
)
ON CONFLICT (dataset_key, version) DO NOTHING;

INSERT INTO evaluation.cases (
    id, dataset_id, case_key, input_json, expected_json, assertions_json,
    data_snapshot_ref
)
VALUES
    ('00000000-0000-7000-8000-000000000e0c01'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-01',
     '{"question":"Evaluate the frozen fixture for BA-01","region_codes":["CN_EAST"],"date_range":{"start":"2025-01-22","end_exclusive":"2025-01-29","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"509476.21","previous_value":"56000.00","absolute_change":"453476.21"}',
     '{"positive_growth":true}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c02'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-02',
     '{"question":"Evaluate the frozen fixture for BA-02","region_codes":["CN_EAST"],"date_range":{"start":"2025-03-31","end_exclusive":"2025-04-07","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"57400.00","previous_value":"70000.00","absolute_change":"-12600.00","change_rate":"-0.1800"}',
     '{"change_rate":"-0.1800"}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c03'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-03',
     '{"question":"Evaluate the frozen fixture for BA-03","region_codes":["CN_EAST"],"date_range":{"start":"2025-01-15","end_exclusive":"2025-01-22","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"56000.00","previous_value":"0.00","absolute_change":"56000.00","change_rate":null}',
     '{"required_annotations":["ZERO_BASELINE"]}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c04'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-04',
     '{"question":"Evaluate the frozen fixture for BA-04","region_codes":["CN_EAST"],"date_range":{"start":"2025-01-08","end_exclusive":"2025-01-15","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"0.00","previous_value":"526499.78","absolute_change":"-526499.78","change_rate":"-1.0000"}',
     '{"required_annotations":["DATA_INSUFFICIENT"],"expected_row_count":0}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c05'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-05',
     '{"question":"Evaluate the frozen fixture for BA-05","region_codes":["CN_EAST"],"date_range":{"start":"2025-01-31","end_exclusive":"2025-02-01","timezone":"Asia/Shanghai"},"compare_mode":"previous_period"}',
     '{"current_value":"73250.53","previous_value":"77316.52","absolute_change":"-4065.99","change_rate":"-0.0526"}',
     '{"timezone":"Asia/Shanghai"}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c06'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-06',
     '{"question":"Evaluate the frozen fixture for BA-06","region_codes":["CN_EAST"],"date_range":{"start":"2025-01-23","end_exclusive":"2025-01-24","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"73418.81","previous_value":"72227.60","absolute_change":"1191.21"}',
     '{"paid_orders_only":true,"source_view":"business.gmv_v1"}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c07'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-07',
     '{"question":"Evaluate the frozen fixture for BA-07","region_codes":["CN_EAST"],"date_range":{"start":"2025-02-10","end_exclusive":"2025-02-11","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"71410.41","previous_value":"76368.69","absolute_change":"-4958.28"}',
     '{"refunded_orders_included":true,"source_view":"business.gmv_v1"}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c08'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-08',
     '{"question":"Evaluate the frozen fixture for BA-08","region_codes":["CN_EAST"],"date_range":{"start":"2025-03-31","end_exclusive":"2025-04-07","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"57400.00","previous_value":"70000.00","absolute_change":"-12600.00","change_rate":"-0.1800"}',
     '{"authorized_regions_only":["CN_EAST"]}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c09'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-09',
     '{"question":"Evaluate the frozen fixture for BA-09","region_codes":["CN_EAST"],"date_range":{"start":"2025-03-31","end_exclusive":"2025-04-07","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"57400.00","previous_value":"70000.00","absolute_change":"-12600.00","change_rate":"-0.1800"}',
     '{"hypotheses_unverified":true,"source_view":"business.gmv_v1"}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8'),
    ('00000000-0000-7000-8000-000000000e0c10'::uuid,
     '00000000-0000-7000-8000-000000000e0b1'::uuid, 'BA-10',
     '{"question":"Evaluate the frozen fixture for BA-10","region_codes":["CN_EAST"],"date_range":{"start":"2025-03-31","end_exclusive":"2025-04-07","timezone":"UTC"},"compare_mode":"previous_period"}',
     '{"current_value":"57400.00"}',
     '{"findings_reference_valid_evidence":true,"source_view":"business.gmv_v1"}',
     'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8')
ON CONFLICT (dataset_id, case_key) DO NOTHING;

-- Least privilege: the evaluation role reads its own schema only.
GRANT USAGE ON SCHEMA evaluation TO agentforge_eval;
GRANT SELECT ON evaluation.datasets, evaluation.cases, evaluation.runs,
    evaluation.case_results TO agentforge_eval;
GRANT SELECT (eval_run_id) ON runtime.tasks TO agentforge_eval;

REVOKE ALL ON evaluation.datasets, evaluation.cases, evaluation.runs,
    evaluation.case_results FROM PUBLIC;
