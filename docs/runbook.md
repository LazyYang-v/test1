# AgentForge P0 — Runbook

Audience: operators running, observing, and troubleshooting a P0 deployment.
Runbook instructions are verified against repository code. The isolated
integration suite uses Docker-backed PostgreSQL; this runbook does not claim a
Compose deployment, live model call, or external trace-export run.

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
- The migration ledger is the only supported recovery record. Re-running the
  migrator on an intact ledger must be a no-op; a missing, renamed, or
  checksum-tampered migration entry must fail closed. Manually clearing
  `agentforge.schema_migrations` to force a replay of historical migration
  SQL is **not** a supported recovery procedure and is forbidden: shipped
  migrations are not required to be raw-SQL replayable. A corrupted or wiped
  ledger must go through manual reconciliation and recovery; it must never
  be "repaired" by replay.
- The frozen dataset is deterministic: same generator version, seed, hash
  algorithm, and ordering always produce the same `data_snapshot_id`
  (`e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8`).
  Re-running seed against a completed snapshot is a no-op.
- Database roles are isolated per process: `agentforge_migrator`,
  `agentforge_api`, `agentforge_worker`, `agentforge_business_ro`,
  `agentforge_eval`. The business read path uses `agentforge_business_ro`,
  which has no write privilege on business tables.

### Backup and migration failure recovery

Before applying migration 0013, make and verify a custom-format backup. Keep
the backup outside the database volume and restrict its permissions:

```sh
umask 077
backup_file="agentforge-before-0013-$(date +%Y%m%d%H%M%S).dump"
docker compose exec -T postgres sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump --host=127.0.0.1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' > "$backup_file"
pg_restore --list "$backup_file" >/dev/null
```

Each migration and its ledger entry run in one transaction. If migration 0013
fails, PostgreSQL rolls back that migration; preserve the database volume and
inspect `docker compose logs migrate`. Do not clear or edit
`agentforge.schema_migrations`, replay historical SQL, or run `docker compose
down -v`. Fix the deployment cause, rerun the migrator, and confirm
`GET /health/ready` reports the expected schema version before serving traffic.
For recovery, initialize a separate PostgreSQL 17 instance with this repository's
PostgreSQL init script and matching role credentials, restore the verified dump,
and validate its ledger and application readiness before a controlled cutover.
Keep the failed database intact until the cutover succeeds.

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

## 9. Bootstrap, identity, and Registry administration

### First Operator

After migration and seed complete, issue the one-time 15-minute Bootstrap
secret from an interactive terminal. The CLI connects with the migrator DSN,
stores only a digest, and refuses to print secrets when stdout is redirected:

```sh
docker compose run --rm -it migrate \
  /app/.venv/bin/agentforge-bootstrap-secret issue-bootstrap
```

Use the secret once with `POST /api/v1/bootstrap`, then log in through
`POST /api/v1/auth/login`. The new Operator has `identity_admin` and
`registry_operator`; the password must contain at least 12 characters. Do not
put the secret in shell history, logs, or a ticket.

### Business users and Registry writes

Create an analyst through `POST /api/v1/admin/identity/users` with the active
Operator token. The endpoint creates an ACTIVE user with only `CN_EAST/read`;
it does not grant Operator access. Region authorization is checked against the
live grant table on task creation and tool execution.

Registry writes use the authenticated API. For local CLI writes, set the API
URL and an Operator bearer token in the current shell, then run
`uv run agentforge-registry ...`. The Registry database role is read-only;
there is no supported direct-DML write path. See `docs/api.md` for the profile,
tool, Agent draft, binding, publish, and retire request contracts.

### Disable and restore an Operator

An active Operator with `identity_admin` can disable another Operator at
`POST /api/v1/admin/identity/operators/{user_id}/disable`. This increments the
target's token version and invalidates existing sessions. For a locked-out
account, a trusted host operator can issue a secret scoped to that account and
action:

```sh
docker compose run --rm -it migrate \
  /app/.venv/bin/agentforge-bootstrap-secret issue-recovery \
  --action disable --user-id <operator-uuid>
```

Send the printed secret once to `POST /api/v1/operator-recovery` with no
`new_password` to disable the target. To restore the disabled Operator, issue
a new secret with `--action restore --user-id <operator-uuid> --rotate`, then
send it with a new password of at least 12 characters. `--rotate` is required
when replacing a consumed or still-live recovery secret. Both commands require
an interactive terminal; recovery secrets expire after 15 minutes and are
single-use. Re-login after restoration because old tokens remain invalid.

Bootstrap, recovery issuance/rotation, denied attempts, and Operator state
changes are audited without storing secret values or passwords.
