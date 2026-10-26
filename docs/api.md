# AgentForge P0 — API Reference

Source of truth: `apps/api/src/agentforge_api/`. Every claim below is derived
from that code; where behavior is conditional it is stated explicitly.

Base URL: `http://127.0.0.1:8000` (default `AGENTFORGE_API_PORT` in Compose).

## Authentication

All `/api/v1/*` endpoints except `POST /api/v1/auth/login` require a bearer
JWT (HS256) issued by the login endpoint. Tokens are short-lived:
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
  "regions": [{ "region_code": "CN_EAST", "permission": "read" }]
}
```

Revoked grants never appear here. The claim list is resolved live from
`identity.region_grants` on each request — JWT claims never carry region
permissions, so revocation is effective on the next request.

### `GET /api/v1/regions/{region_code}/access`

Returns whether the caller currently holds an active grant for the region.
Denied access produces a `region.authorization` audit event with decision
`denied`.

## Unified error body

Every error returned by the API has the same JSON shape:

```json
{
  "code": "REGION_ACCESS_DENIED",
  "message": "Access to this region is denied",
  "retryable": false,
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
`result` and `error`. Result/error payloads are **redacted (null)** when the
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
