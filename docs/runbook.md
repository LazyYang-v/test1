# AgentForge P0 — Runbook

Audience: operators running, observing, and troubleshooting a P0 deployment.
Everything here is verified against the code at `integration/p0`; items that
require an environment this repository's sandbox cannot provide are marked
explicitly.

## 1. Cold start (required services)

```sh
export POSTGRES_PASSWORD=...                      # optional; has safe defaults
export AGENTFORGE_MIGRATOR_DB_PASSWORD=...
docker compose up --build -d --wait --wait-timeout 180
```

Required services: `postgres`, `migrate`, `seed`, `api`, `web`, `worker`.

Startup order is enforced by Compose conditions:

1. `postgres` healthy,
2. `migrate` runs all migrations as `agentforge_migrator` (exits 0),
3. `seed` generates the deterministic frozen dataset and loads it
   (`agentforge-dataset` + `agentforge-seed`; idempotent — a re-run against a
   completed snapshot is a no-op and the content hash must not change),
4. `api` and `worker` start only after both migrate and seed complete.

Health endpoints: `GET /health/live` (process up) and `GET /health/ready`
(schema version in the migration ledger must equal the expected version).
The API container runs as uid 10001 and connects as the least-privileged
`agentforge_api` role; it never runs migrations.

Web UI: `http://localhost:3000` (default `AGENTFORGE_WEB_PORT`), API at
`http://localhost:8000` (`AGENTFORGE_API_PORT`).

## 2. Optional observability profile

Tempo, Prometheus, Grafana, and Redis exist **only** under the optional
`observability` profile; the default cold start neither starts nor requires
them:

```sh
docker compose --profile observability up -d
```

Grafana (anonymous viewer) is exposed on `AGENTFORGE_GRAFANA_PORT` (default
3001) with Tempo and Prometheus datasources pre-provisioned.

To ship traces from the platform, set on `api` and `worker`:

```sh
AGENTFORGE_OTEL_ENABLED=true
AGENTFORGE_OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318
```

Both default to off (`otel_enabled: false`), so a stock deployment exports
nothing.

## 3. Migration and seed repeatability

- Migrations are ordered, ledgered, and idempotent to re-run (`up to date`).
  The API refuses readiness when the ledger version mismatches, so a
  half-migrated database never serves traffic.
- The frozen dataset is deterministic: same generator version, seed, hash
  algorithm, and ordering always produce the same `data_snapshot_id`
  (`e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8`).
  Re-running seed against a completed snapshot is a no-op.
- Database roles are isolated per process: `agentforge_migrator`,
  `agentforge_api`, `agentforge_worker`, `agentforge_business_ro`,
  `agentforge_eval`. The business read path uses `agentforge_business_ro`,
  which has no write privilege on business tables.

## 4. Test tiers

```sh
# Offline: deterministic fake model, no PostgreSQL (fast, always runnable)
uv run pytest tests/unit -q

# Integration: real PostgreSQL 17 via ephemeral Docker, real migrations,
# HTTP API + worker seam, deterministic fake model
uv run pytest tests/integration -q

# Live (never in automated regression): real DeepSeek end-to-end against
# the frozen snapshot. Requires AGENTFORGE_DEEPSEEK_API_KEY and the
# evaluation live flag; see §6.
```

The integration tier boots its own ephemeral `postgres:17` container and
tears it down; it needs a working Docker daemon and nothing else.

## 5. Observing and troubleshooting a task

Every task has a `trace_id` (currently the task id as text). It appears in:

- structured JSON API/worker logs (redacted: no credentials, no full prompts,
  no raw business rows),
- `runtime.task_events.trace_id`,
- `harness.model_invocations` and `harness.tool_invocations`
  (provider request id, model version, parameters, token usage, cost,
  duration),
- `audit.audit_events.trace_id`.

To explain a **failed** task:

```sql
-- lifecycle: what happened and in what order
SELECT task_event_seq, event_type, payload_json, occurred_at
FROM runtime.task_events WHERE task_id = '<task uuid>' ORDER BY task_event_seq;

-- per-step outcomes and error codes
SELECT step_key, status, result_ref, started_at, completed_at
FROM runtime.step_executions WHERE task_id = '<task uuid>' ORDER BY step_key;

-- model and tool calls with costs and provider request ids
SELECT invocation_key, provider, model_name, status, error_code,
       input_tokens, output_tokens, estimated_cost, duration_ms
FROM harness.model_invocations WHERE task_id = '<task uuid>';
SELECT invocation.invocation_key, tool.tool_key, invocation.status,
       invocation.error_code, invocation.started_at, invocation.finished_at
FROM harness.tool_invocations AS invocation
JOIN registry.tool_versions AS tool ON tool.id = invocation.tool_version_id
WHERE invocation.task_id = '<task uuid>';

-- security-relevant denials (region checks, unbound tools, bad inputs)
SELECT occurred_at, action, decision, reason_code, metadata_json
FROM audit.audit_events WHERE trace_id = '<trace_id>' ORDER BY id;
```

Interpretation guide (frozen error classification):

- Retryable (`retryable=true` family): provider timeouts/5xx/rate limits,
  tool timeouts → the task enters `retry_wait` with `next_retry_at`
  (base 30 s + jitter) and is re-claimed automatically; attempts are bounded
  by `max_attempts`.
- Terminal: `MODEL_OUTPUT_INVALID` (schema repair budget exhausted),
  `QUERY_PLAN_INVALID`, `REPORT_*`, `REGION_ACCESS_DENIED`,
  `TOOL_NOT_BOUND`, `TOOL_INPUT_INVALID`, `BUDGET_EXCEEDED`,
  `DEADLINE_EXCEEDED`, `ATTEMPTS_EXHAUSTED` → task `failed` with the code in
  `error_code` and diagnostics preserved. Terminal states are immutable.
- Crash recovery: a lost lease marks the attempt `abandoned`, the task moves
  through `recovering`, and a new attempt replays confirmed step executions
  (memoized) instead of re-running them; duplicate tool calls are deduplicated
  by `(task_id, invocation_key)`. Recovery never fabricates success: an
  unrecoverable state fails the task with diagnostics.

Audit action families (append-only, never updated): `authentication.failed`,
`region.authorization`, `task.created`, `task.cancel`,
`tool.invocation`, `agent.version.published` / `agent.version.retired`
(registry triggers), `evaluation.run.started`.

## 6. Live evaluations (explicit, budgeted)

Live mode is off by default. To enable on the API:

```sh
AGENTFORGE_LIVE_EVALUATIONS_ENABLED=true
AGENTFORGE_LIVE_EVALUATION_CALL_BUDGET_LIMIT=50   # hard server-side ceiling
```

Request a run with `model_mode: "live"` and a `live_call_budget` ≤ the
ceiling. The worker charges each live model call atomically against
`evaluation.runs.live_calls_used` under an active-lease check; when the
budget is exhausted the call fails and the task fails — spend is capped, and
the DeepSeek API key is only read from the worker environment
(`AGENTFORGE_DEEPSEEK_API_KEY`), never stored in the registry.

## 7. Known environment-blocked evidence

The following Release Gate items require a Docker-capable (and, for live
runs, network-capable) host. They are tracked, with exact commands, in
`docs/verification/release-gate-evidence.md`; no evidence is claimed for
them until executed:

- full `docker compose up --wait` cold-start re-verification at the final
  integrated commit (last recorded run: 2026-10-26 at migrations 0001–0005),
- ≥3 live DeepSeek end-to-end runs with retained request/response evidence,
- trace export through the observability profile.

## 8. Data safety notes

- Terminal task states (`completed`, `failed`, `cancelled`) are irreversible;
  no API or worker path reopens them. P0 "rerun" = create a new task.
- Evidence rows are immutable and snapshot-bound: a task's evidence all
  belongs to the `data_snapshot_id` bound at creation.
- The semantic layer is the only data path: model output is a metric query
  plan, compiled to parameterized read-only SQL with platform-injected region
  filters. There is no free-SQL path to the business tables.
