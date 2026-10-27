# AgentForge P0 — API Reference

Source of truth: `apps/api/src/agentforge_api/`. Every claim below is derived
from that code; where behavior is conditional it is stated explicitly.

Base URL: `http://127.0.0.1:8000` (default `AGENTFORGE_API_PORT` in Compose).

## Authentication

All `/api/v1/*` endpoints except `POST /api/v1/auth/login`, `POST /api/v1/bootstrap`,
and `POST /api/v1/operator-recovery` require a bearer JWT. Bootstrap and recovery
are unauthenticated only because each requires a short-lived, one-time secret.
`jwt_ttl_seconds` is capped at 900 seconds (15 minutes). There is no refresh
token and no session table; a new token is obtained by logging in again.

### `POST /api/v1/auth/login`

Request:

```json
{ "email": "user@example.com", "password": "..." }
```

Response `200`:

```json
{ "access_token": "<jwt>", "token_type": "bearer", "expires_in": 900 }
```

Errors:

| Status | Code | When |
| --- | --- | --- |
| 401 | `UNAUTHENTICATED` | Unknown email, wrong password, or disabled user. Writes an `authentication.failed` audit event. |

### `GET /api/v1/auth/me`

Returns the caller's identity and currently active region grants:

```json
{
  "id": "<uuid>",
  "email": "user@example.com",
  "display_name": "...",
  "regions": [{ "region_code": "CN_EAST", "permission": "read" }],
  "operator_permissions": ["registry_operator"]
}
```

Revoked grants never appear here. The claim list is resolved live from
`identity.region_grants` on each request — JWT claims never carry region
permissions, so revocation is effective on the next request.

### `GET /api/v1/regions/{region_code}/access`

Returns whether the caller currently holds an active grant for the region.
Denied access produces a `region.authorization` audit event with decision
`denied`.

## Bootstrap and Operator administration

Operator permissions and account status are read from PostgreSQL during each
administrative request. JWTs do not contain permissions. Disabling an Operator
increments `token_version`, invalidating every outstanding token.

### `POST /api/v1/bootstrap` → `201 Created`

One-time first-Operator creation. Obtain a 15-minute secret with the local
`agentforge-bootstrap-secret issue-bootstrap` command; the database stores only
its digest. The command displays secrets only to an interactive terminal.

```json
{
  "bootstrap_secret": "<one-time secret>",
  "email": "operator@example.com",
  "display_name": "AgentForge Operator",
  "initial_password": "<12 or more characters>"
}
```

Success returns the new user ID, normalized email, display name, and both
Operator permissions: `identity_admin` and `registry_operator`. A consumed,
expired, or invalid secret cannot create another Operator.

### `POST /api/v1/admin/identity/users` → `201 Created`

Requires `identity_admin`. Creates an ACTIVE user and exactly one initial
grant: `CN_EAST/read`. The user receives no Operator permission.

```json
{
  "email": "analyst@example.com",
  "display_name": "Analyst",
  "initial_password": "<12 or more characters>"
}
```

The response includes the user ID, status, region grant, and an empty
`operator_permissions` list. Duplicate email returns `409 EMAIL_ALREADY_EXISTS`.

### `POST /api/v1/admin/identity/operators/{user_id}/disable`

Requires `identity_admin`. Disables an active Operator and invalidates existing
sessions. Returns `{ "id": "<uuid>", "status": "disabled" }`.

### `POST /api/v1/operator-recovery`

Unauthenticated break-glass endpoint. The local CLI issues a 15-minute secret
scoped to one Operator and one action. Each secret is single-use. For `restore`,
send a new password; for `disable`, omit it.

```json
{
  "recovery_secret": "<one-time secret>",
  "new_password": "<required only for restore>"
}
```

Returns the target ID and resulting status. Recovery actions, denials, secret
issuance, and rotation are audited without recording secret values or passwords.

## Authenticated Registry writes

All `/api/v1/admin/registry/*` routes require a live `registry_operator`
permission. `agentforge-registry` sends writes here with its bearer token;
`agentforge_registry_cli` has read-only Registry table privileges after
migration 0013.

Relative route paths below are under `/api/v1/admin/registry`.

- `POST /model-profiles`: creates a model profile. `credential_ref` must be a
  `secret://` URI; `config_json` rejects embedded credential fields.
- `POST /tools`: creates a tool version with input/output JSON Schemas, policy,
  execution kind, and timeout.
- `POST /agents`: creates a draft with `agent_key`, `version`, `name`,
  `graph_key`, `model_profile`, and `config_json`. `created_by` is set from the
  authenticated principal.
- `PUT /agents/{version_id}/tools/{tool_id}`: binds a tool to the draft; policy
  overrides may only tighten the base policy.
- `POST /agents/{version_id}/publish`: validates and publishes the draft,
  freezes its model configuration, and records the authenticated actor.
- `POST /agents/{version_id}/retire`: retires a published version.

Successful write responses return the fields documented by each operation:
model profiles return `id` and `profile_key`; tools return `id`, `tool_key`, and
`version`; draft Agents return `id`, `agent_key`, `version`, and `status`;
bindings return both version IDs and `policy_override`; publish returns the
published Agent fields, frozen `model_config_snapshot`, `config_hash`, and
`published_at`; retire returns `id` and `status`. Registry audit metadata records
resource keys and IDs, never request secrets or config payloads.

These routes return `403 OPERATOR_PERMISSION_REQUIRED` without the required
permission, `409 REGISTRY_CONFLICT` for conflicting lifecycle or uniqueness
state, and `400 INVALID_REQUEST` for invalid configuration.

## Unified error body

Every error returned by the API has the same JSON shape:

```json
{
  "error": {
    "code": "REGION_ACCESS_DENIED",
    "message": "Access to this region is denied",
    "retryable": false,
    "details": {}
  },
  "request_id": "<request-scoped id>"
}
```

`request_id` is generated per request (`X-Request-ID` is honored when
supplied) and is included in the structured access log. 429 responses carry a
`Retry-After` header. Validation failures (422 from request-model parsing) are
translated into the same body with code `INVALID_REQUEST`.

## Error code table

| Code | HTTP | Retryable | Raised by |
| --- | --- | --- | --- |
| `UNAUTHENTICATED` | 401 | no | Missing/expired/invalid JWT, bad login credentials, expired SSE stream token |
| `REGION_ACCESS_DENIED` | 403 | no | Task regions outside the caller's active grants |
| `INVALID_REQUEST` | 400 | no | Malformed body or query, missing/too-long Idempotency-Key, unknown agent_key, bad Last-Event-ID, list filter errors, live-mode misuse |
| `BOOTSTRAP_UNAVAILABLE` | 403 | no | Missing or incorrect Bootstrap secret |
| `BOOTSTRAP_EXPIRED` | 410 | no | Bootstrap secret expired |
| `BOOTSTRAP_CONSUMED` | 409 | no | Bootstrap secret already consumed |
| `BOOTSTRAP_ALREADY_INITIALIZED` | 409 | no | An Operator already exists |
| `RECOVERY_UNAVAILABLE` | 403 | no | Missing or incorrect recovery secret |
| `RECOVERY_EXPIRED` | 410 | no | Recovery secret expired |
| `RECOVERY_CONSUMED` | 409 | no | Recovery secret already consumed |
| `OPERATOR_PERMISSION_REQUIRED` | 403 | no | Missing live Operator permission |
| `EMAIL_ALREADY_EXISTS` | 409 | no | Identity email already exists |
| `OPERATOR_NOT_FOUND` | 404 | no | Operator does not exist |
| `OPERATOR_NOT_ACTIVE` | 409 | no | Disable target is already inactive |
| `OPERATOR_STATE_CONFLICT` | 409 | no | Recovery target changed state |
| `MODEL_PROFILE_NOT_FOUND` | 404 | no | Registry draft references an unknown model profile |
| `TOOL_VERSION_NOT_FOUND` | 404 | no | Requested tool version does not exist |
| `AGENT_VERSION_NOT_FOUND` | 404 | no | Requested Agent version does not exist |
| `AGENT_VALIDATION_FAILED` | 409 | no | Draft is not valid for publication |
| `AGENT_VERSION_NOT_PUBLISHED` | 409 | no | Retirement requires a published Agent version |
| `INVALID_POLICY_OVERRIDE` | 400 | no | Tool policy override would loosen its base policy |
| `REGISTRY_CONFLICT` | 409 | no | Registry uniqueness or lifecycle conflict |
| `TASK_NOT_FOUND` | 404 | no | Task missing **or** owned by another user (existence is not leaked) |
| `IDEMPOTENCY_CONFLICT` | 409 | no | Same Idempotency-Key used with a different canonical request |
| `INVALID_STATE_TRANSITION` | 409 | no | Cancel rejected: terminal task, cancel already requested, stale optimistic `expected_version`, invalid state |
| `EVALUATION_DATASET_NOT_FOUND` | 404 | no | Unknown or inactive dataset_key |
| `EVALUATION_RUN_NOT_FOUND` | 404 | no | Unknown evaluation run id |
| `EVALUATION_DATASET_EMPTY` | 409 | no | Dataset has no registered cases |
| `LIVE_EVALUATIONS_DISABLED` | 403 | no | `model_mode=live` while the server flag is off |
| `IDEMPOTENCY_RETRY` | 503 | **yes** | Idempotency record still `processing` (first request in flight or interrupted) |
| `DATA_SNAPSHOT_UNAVAILABLE` | 503 | **yes** | No completed business dataset snapshot to bind the task to |
| `EVALUATION_SERVICE_UNAVAILABLE` | 503 | **yes** | The svc-eval service identity is missing or inactive |
| `RATE_LIMITED` | 429 | **yes** | Per-user token bucket exceeded, or too many concurrent SSE streams; always carries `Retry-After` |
| `INTERNAL_ERROR` | 500 | no | Unhandled server error (details only in logs) |

Rate limiting is **per-process** (in-process token bucket keyed by user);
deployment scale-out would need a shared store, which P0 explicitly excludes.

## Tasks

### `POST /api/v1/tasks` → `202 Accepted`

Creates a business-analysis task. Requires header `Idempotency-Key`
(mandatory, ≤255 chars).

Request (summary — see `tasks.py` `DateRange`/`TaskInput`/execution models):

```json
{
  "task_type": "business_analysis",
  "agent_key": "business-analyst",
  "input": {
    "question": "华东最近 7 天 GMV 环比为什么变化?",
    "region_codes": ["CN_EAST"],
    "date_range": { "start": "2025-03-31", "end_exclusive": "2025-04-07", "timezone": "UTC" },
    "compare_mode": "previous_period"
  },
  "execution": { "max_attempts": 3, "timeout_seconds": 300 },
  "expected_version": null
}
```

Semantics:

- `region_codes` must be a subset of the caller's **currently active** grants
  (live check, not JWT claims). Otherwise `403 REGION_ACCESS_DENIED` and a
  `region.authorization` audit event.
- Date range is left-closed/right-open in the given IANA `timezone`, capped by
  the request model (≤90 calendar days per the P0 contract).
- The task binds to the latest **completed** data snapshot
  (`data_snapshot_id`); without one → `503 DATA_SNAPSHOT_UNAVAILABLE`.
- Idempotency: same key + same canonical request replays the **exact** stored
  202 response (including the in-flight window — a concurrent first request
  yields `503 IDEMPOTENCY_RETRY` until it completes). Same key + different
  request → `409 IDEMPOTENCY_CONFLICT`.
- The creation transaction also persists the first `task.created` event
  (seq 1) and a `task.created` audit row.

Response `202`:

```json
{
  "task_id": "<uuid>",
  "status": "queued",
  "agent_version": "<resolved published version>",
  "created_at": "...",
  "links": { "self": "/api/v1/tasks/<id>", "events": "/api/v1/tasks/<id>/events" }
}
```

### `GET /api/v1/tasks` (list)

Query params: `status`, `limit` (≤100, default 20), `offset` (≤100000),
`created_after`/`created_before` (must include a timezone,
`created_after < created_before`). Returns the caller's own tasks only,
ordered newest first, as `{"items": [...], "total": n}`. Unknown status →
400.

### `GET /api/v1/tasks/{task_id}`

Returns status, resolved `agent_version`, `current_attempt_no`, progress,
`input` (the stored canonical request: `question`, `region_codes`,
`date_range`, `compare_mode`), `result` and `error`. Result/error payloads are
**redacted (null)** when the
caller's region grant for the task's regions has been revoked — ownership
alone does not re-authorize stored results. A foreign task is a 404.

### `POST /api/v1/tasks/{task_id}/cancel` → `202 Accepted`

Cooperative cancellation. Body: `{ "reason": "...", "expected_version": n }`.

- `queued`/`retry_wait` → task becomes `cancelled` immediately (append-only:
  `task.cancel.requested` event plus immediate `task.cancelled`).
- `running`/`recovering` → `task.cancel.requested`; the worker stops at the
  next node boundary / before the next tool call and the task converges to
  `cancelling` → `cancelled`.
- Terminal tasks, already-requested cancels, stale `expected_version`, and
  blank reasons are rejected (`409`/`409`/`409`/`400`), with
  `details.current_version` on version conflicts.
- Ownership is enforced; foreign tasks are 404.

### `GET /api/v1/tasks/{task_id}/events` (SSE)

Server-sent event stream of the task's event log.

- Content type `text/event-stream`; each frame's `id:` is the persistent
  `task_event_seq` (strictly increasing per task, assigned in the persisting
  transaction).
- Reconnection: send `Last-Event-ID: <seq>` (header) and the stream replays
  every persisted event with `seq > <seq>` before continuing live. This makes
  delivery **at-least-once**; clients must deduplicate by `id`.
- Wakeups use `pg_notify` on channel `agentforge_task_events` fired inside
  the event-persisting transaction (delivered after commit) with a **10
  second** fallback poll, so committed events are never lost to a missed
  notification, a dropped listen connection, or an API restart.
- Authorization: owner-scoped (foreign task → 404). The stream periodically
  revalidates the JWT; expiry closes the stream (`401 UNAUTHENTICATED` on the
  next connect). Concurrent streams per user are limited; excess streams get
  `429 RATE_LIMITED` with `Retry-After: 1`.

## Evaluations

Evaluation tasks are created on behalf of the dedicated `svc-eval` service
account (fixture-region read-only) and never appear in normal users' task
lists.

### `GET /api/v1/evaluations/datasets`

Lists active evaluation datasets with version, content hash,
`data_snapshot_id`, status and case count.

### `POST /api/v1/evaluations/runs` → `202 Accepted`

Requires `Idempotency-Key`. Body:

```json
{
  "dataset_key": "p0-core-business-analysis",
  "agent_key": "business-analyst",
  "model_mode": "fake",
  "live_call_budget": null
}
```

- `model_mode` defaults to `fake`. `live` requires the server-side flag
  (`live_evaluations_enabled`) — otherwise `403 LIVE_EVALUATIONS_DISABLED` —
  and a `live_call_budget` within the configured limit (`400` otherwise).
- Each case whose frozen `data_snapshot_ref` does not match the dataset
  snapshot, or whose regions are outside svc-eval's grants, becomes a direct
  `failed` case result with `DATA_SNAPSHOT_MISMATCH` / `REGION_ACCESS_DENIED`
  instead of creating a task.
- Remaining cases create one queued runtime task per case (bound to the run
  via `eval_run_id`, one attempt, 10-minute deadline).
- Response `202`: `eval_run_id`, `status`, `case_count`, `task_count`,
  `direct_failures`.
- Live budget enforcement happens in the worker: each live model call
  atomically increments `live_calls_used` under a fencing/lease check; once
  the budget is exhausted the call fails and the task fails rather than
  overspending.

### `GET /api/v1/evaluations/runs/{run_id}`

Returns run status, `model_mode`, budget/usage, timestamps, `summary`
(total/passed/failed/score) and per-case results (`case_key`, `task_id`,
`status`, `score`, `failure_codes`, `duration_ms`). Evaluation only **reads**
task results to judge them; it never rewrites task outcomes.

## Health

- `GET /health/live` → `{"status":"alive"}` when the process is up.
- `GET /health/ready` → `{"status":"ready", "expected_schema_version": N,
  "applied_schema_version": N}`; refuses readiness (non-200) when the
  migration ledger does not match the expected schema version, so traffic
  never reaches a half-migrated database.
