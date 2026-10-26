# 05: Task 创建、幂等、查询

**What to build:** `POST /api/v1/tasks` 完整落地：校验身份、区域子集、日期范围（≤90 天、左闭右开）、已发布 Agent 解析，创建 Task（绑定 data_snapshot_id 与 authorization_snapshot）返回 202 并落首个事件。幂等键全语义：同键同请求重放同一响应（含并发处理窗口），同键异请求 409。任务详情与分页列表查询。

**Blocked by:** 02、03、04

**Status:** claimed

Integration: latest integration/p0 (`2b41af6`) is merged into this branch (`0b94c78`); pending final integration merge and full-suite verification by the coordinator. Keep Status claimed until those steps pass.

- [x] 合法请求 202 + task_id + links；Task 状态 queued，task.created 事件落库
- [x] agent_key 解析不到已发布版本 → 拒绝；region_codes 非当前授权子集 → 403 REGION_ACCESS_DENIED；日期范围超 90 天 → 拒绝
- [x] 同幂等键同规范化请求重放同一 202 响应，含首个请求仍在处理中的并发窗口（RT-02）
- [x] 同幂等键不同请求 → 409 IDEMPOTENCY_CONFLICT（RT-03）
- [x] 创建事务原子：任何失败不留半创建 Task
- [x] `GET /tasks/{id}` 返回状态/版本/current_attempt_no/进度占位；他人任务返回 404 不泄露存在性
- [x] 任务列表按用户、状态、日期分页
- [x] 统一错误体（code/message/retryable/request_id）覆盖全部错误路径

## Comments

2026-10-26: Review corrections implement ISO calendar dates (`start`, `end_exclusive`) with a validated IANA `timezone`, a 90-calendar-day maximum, the P0 `business_analysis` task type and `previous_period` comparison default. Creation persists and replays `agent_version`, the database `created_at`, and self/events links. A private SECURITY DEFINER insert guard locks and rechecks the published Agent without granting Registry DML to the API. PostgreSQL lock-observed HTTP tests verify both retirement orderings and all four combinations of same/different hash and new/expired idempotency key, including exact response bytes and atomic Task/event/audit persistence. Happy-path fixtures use the frozen CN_EAST 2025-03-31–2025-04-07 window.

Verification after merging integration/p0 `2b41af6890e9b3c85ec267a82f8dbb74357aece6` into ticket branch at `0b94c782c1387d3a50132b60b9b38dca9aa93ee5`:

- `uv run pytest tests/integration/test_api_tasks.py tests/integration/test_api_readiness.py tests/integration/test_migration_0001.py tests/integration/test_registry_cli.py -q --tb=short` → `56 passed in 183.62s (0:03:03)`.
- `uv run pytest -q --tb=short` → `83 passed in 211.49s (0:03:31)`; includes `test_registry_migration_is_present_in_schema_version_six`.
- `git diff --check integration/p0...HEAD` → clean.

Migrations 0001–0005 are unchanged. Ticket implementation is ready for coordinator merge; keep the ticket claimed until final integration merge and full-suite verification pass. The separate Registry policy follow-up is tracked by ticket 04.
