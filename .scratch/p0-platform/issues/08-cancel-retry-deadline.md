# 08: 取消、重试、Deadline、错误分类

**What to build:** 任务生命周期逆境路径全部落地：取消（queued 直接 cancelled、running 协作式经 cancelling）、冻结的错误分类表驱动 retry_wait/failed、退避调度（next_retry_at）、Attempt 耗尽与 Deadline 终止。取消是协作式的——已发出的调用无法强制中止，正确性靠 fencing 保证。

**Blocked by:** 06

**Status:** resolved

- [x] queued 任务取消直接 cancelled；running 进入 cancelling，执行者在节点边界与工具调用前检测后安全停止（RT-10）
- [x] 取消后不再发起新的模型/工具调用；cancelling 下 Worker 失联走 recovering
- [x] 可重试错误（Fake 注入 provider 超时/5xx）→ retry_wait + next_retry_at（30s+抖动）；到期自动回 queued 被重新认领
- [x] 不可重试错误（Schema 修复耗尽、权限拒绝、BUDGET_EXCEEDED、内部不变量违反）直接 failed 带错误码
- [x] 最大 Attempt 耗尽 → failed（RT-11）；Deadline 到期 → failed(DEADLINE_EXCEEDED)，由节点边界检查与扫描器双路执行（RT-12）
- [x] 取消与完成并发竞争只产生一个合法终态（RT-15）
- [x] 取消请求（含乐观版本）冲突时返回 409 INVALID_STATE_TRANSITION

## Comments

2026-10-26: Implementation claimed on `ticket/08-cancel-retry-deadline` (branched from `integration/p0` tip `1cf9f50`). Scope: API cancellation route and read-model projection, cooperative worker cancellation at node boundaries and in the Tool Gateway, the frozen retry/deadline classification table, retry backoff, and the recovery sweep. No SSE, fault injection, metrics, rate limiting, or Release Gate work is included.

### Verification (2026-10-26, pending integration)

- Branch: `ticket/08-cancel-retry-deadline`; implementation commits `f45b45d`, `eb84912`; merged ticket 07 real metrics/read-only business DSN from `integration/p0` tip `ac8ee23` in merge commit `f8d31bc`; combined worker test DSNs in `56505a2`; final authorization/lifecycle fixes in `9f0330d` and `798cecd`.
- Merge: checked `integration/p0` at `ac8ee23` and merged it into this ticket branch. Resolved `tests/integration/worker_support.py` by preserving both ticket 07's configurable date range/real dataset loader and ticket 08's attempt/deadline task options. The real `MetricsQueryExecutor` and `agentforge_business_ro` DSN are preserved alongside lifecycle behavior.
- Migration: none. Migrations remain 0001-0008 and `agentforge_core.schema_version.SCHEMA_VERSION` remains 8; 0001-0008 were not edited.
- Full suite after merging ticket 07 and before the final security/lifecycle review fixes: `uv run pytest -q --tb=short` -> `413 passed in 1032.81s (0:17:12)` on continuous migrations 0001-0008 against real Docker PostgreSQL; exit code 0. The merger will run the full suite after the final fixes.
- Latest focused verification after the final fixes: `tests/integration/test_api_cancel.py` and `tests/integration/test_worker_retry_deadline.py` -> `22 passed in 410.64s`; `tests/unit/test_worker_errors.py tests/unit/test_worker_config.py` -> `23 passed in 0.42s`; four focused regressions -> `4 passed in 68.73s` (grant revocation/redaction, transient heartbeat recovery, cancellation-before-deadline worker+scanner precedence, and expired queued deadline scanner). The grant regression failed before its implementation and passed after it. `git diff --check integration/p0..HEAD` is clean.
- HTTP evidence (`tests/integration/test_api_cancel.py`): `POST /api/v1/tasks/{id}/cancel` returns 202 with `status=cancelled` for a queued Task and 202 with `status=cancelling` for a running Task; foreign owner -> 404 `TASK_NOT_FOUND`; stale `expected_version` -> 409 `INVALID_STATE_TRANSITION` with `details.current_version`; terminal Task, repeated request, and blank reason -> 409/409/400; no side-effect event on rejected requests. After completion, revoking a live region grant preserves owner-scoped task metadata (GET 200) but returns `result: null`; it also redacts error code/message for failed tasks whose region grant has been revoked. Authorization snapshots remain audit-only.
- DB evidence: a queued cancel commits contiguous events seq 1-3 (`task.created`, `task.cancel.requested`, `task.cancelled`) with exactly one terminal event; a running cancel commits seq 1-5 (`task.created`, `task.attempt.started`, `task.cancel.requested`, `task.cancelling`, `task.cancelled`), abandons the Attempt with `CANCELLED`, leaves zero steps, and issues exactly one in-flight model call with no repair or later node; a later worker cannot claim the terminal Task.
- Retry/deadline evidence (`tests/integration/test_worker_retry_deadline.py`): `MODEL_PROVIDER_UNAVAILABLE` -> `retry_wait`, `next_retry_at` 29-40s ahead, Attempt 1 `failed` at fencing epoch 1; a not-due claim returns False; a due claim creates Attempt 2 at epoch 2 and completes. `max_attempts` exhaustion -> `failed` with one `task.failed`; invalid JSON after bounded repairs -> `failed MODEL_OUTPUT_INVALID` with no `task.retry_scheduled`. Deadline -> `failed DEADLINE_EXCEEDED` from both the scanner (queued Task, no Attempt) and the worker node boundary (running Attempt marked failed, no completion). `cancelling` + lost lease -> `recovering` -> `cancelled`; `recovering` -> `queued` when attempts remain, or `failed ATTEMPTS_EXHAUSTED`. A cancel-vs-complete race produces exactly one terminal event and a matching Attempt outcome. A transient heartbeat database exception stops graph work but leaves the live Attempt running until lease expiry, after which the scanner recovers/requeues it and a new Attempt claims at fencing epoch 2. A cancellation event committed before deadline expiry wins consistently in `stop_reason`, the fenced worker terminal transition, and scanner reconciliation; worker and scanner paths each produce one `task.cancelled` and no `task.failed`. Stale Attempts are rejected after retry or cancel and cannot write steps or terminal results.
- Classification evidence (`tests/unit/test_worker_errors.py`): provider timeout/unavailable/5xx/rate-limit and tool timeout are retryable; `MODEL_OUTPUT_INVALID`, `REPORT_*`, `REGION_ACCESS_DENIED`, `TOOL_NOT_BOUND`, `TOOL_INPUT_INVALID`, `BUDGET_EXCEEDED`, and internal/unknown codes are terminal.
- Deviation: the API role has SELECT/INSERT but no UPDATE on `runtime.tasks` (an existing security test enforces this), so `POST .../cancel` is append-only: it writes `task.cancel.requested` (plus an immediate `task.cancelled` for non-executing Tasks) under the per-Task advisory lock, and GET/list project the effective `cancelled`/`cancelling` status until the Worker scanner materializes the status column and increments `version`. For that short window the projected status is terminal while the stored `version` is still the pre-request value; subsequent requests are rejected by already-requested/terminal checks.
- Status left `claimed`; this handoff is pending integration by the merger. No changes were made to the main checkout; worktrees/branches were preserved.

## Resolution

2026-10-27 (orchestrator): ticket branch merged into `integration/p0` at `12cfb4a` (+ follow-ups `9f0330d`, `798cecd`, `2185d8a` predate the merge). The pre-merge verification handoff in this file (22 cancel/deadline integration tests, 23 unit classification tests, four focused regressions, full-suite 413 passed at handoff) is the recorded evidence; acceptance boxes above are checked against that handoff. Post-merge full-suite reruns at later commits were recorded under tickets 09-15 verification threads.
