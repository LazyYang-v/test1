# 06: 第一发曳光弹——任务完整跑通（指标桩）

**What to build:** Worker 认领 queued 任务并完整执行四节点图（plan_metrics → execute_metrics → compose_analysis → validate_report）：Fake Model 产出查询计划与分析说明、指标节点先用确定性桩、Tool Gateway 九步安全骨架就位、每步确认写 Step Execution 与事件、任务进入 completed 并持久化符合报告 Schema 的结果。这是贯穿 Runtime/Harness/图/网关的第一条完整路径——数值尚来自桩，07 号换真实查询。

**Blocked by:** 05

**Status:** claimed

- [ ] Worker 以 SKIP LOCKED 认领；两 Worker 并发领取只有一个有效租约（RT-04）
- [ ] 心跳按间隔续期租约（RT-05）；Attempt 记录 worker/lease_token/fencing_epoch
- [ ] 四节点顺序执行；每步确认在事务内验证租约后写 step_executions（稳定 step_key，如 plan_metrics:0）+ task_event
- [ ] Fake Model 输出可脚本化；非法 JSON 经校验有限修复重试、超预算失败而非默认结论（HS-02）
- [ ] Tool Gateway 骨架：工具版本验证、输入 Schema 校验、未绑定工具拒绝（HS-03）、非法参数拒绝（HS-04）、审计与 Trace 记录
- [ ] 结果符合报告 Schema：findings 引用有效证据 ID、hypotheses 标注待验证、limitations 齐全；validate_report 拒绝引用不存在证据（BA-10 结构）
- [ ] 任务 → completed，result_json 与 completed_at 持久化，事件序完整可读
- [ ] 数据库事务短小：节点执行期间不持有 Task 行锁

## Comments

2026-10-26: Implementation started on `ticket/06-tracer-bullet-task-completion` (branched from `integration/p0` tip `9e7dc39`). Scope: migration 0008, `packages/runtime`, `packages/harness`, `apps/worker`, LangGraph in-process MemorySaver, deterministic fake model and the nine-step tool gateway skeleton. Ticket remains claimed until integration verification.

### Verification (2026-10-26, pending integration)

- Branch: `ticket/06-tracer-bullet-task-completion`; commits `f8e71ba`, `018c97b`, `239db3f`, `29dcd36`, `0d93854`.
- Migration: additive `0008_runtime_harness_evidence.sql`; `SCHEMA_VERSION` and readiness/migration expectations are 8; 0001-0007 unchanged.
- Merge: `integration/p0` tip `9e7dc39` merged into the ticket branch ("Already up to date").
- Full suite: `uv run pytest -q --tb=short` -> `348 passed in 279.89s (0:04:39)` on the continuous 0001-0008 sequence.
- Review fixes: `load_agent` now takes provider/model_name/timeout/max_output_tokens from the frozen `model_config_snapshot` (profile only as legacy fallback); a regression test publishes via the Registry CLI, mutates the profile to a different provider/name/timeout, and proves the worker still records `fake`/`fake-v1` and times out at the frozen 50ms. `merge_policy` now accepts `require_*` base `false -> true` while rejecting `false -> false`, matching migration 0007.
- `diff --check integration/p0...HEAD` -> clean.
- Evidence: `POST /api/v1/tasks` -> real worker -> `GET /api/v1/tasks/{id}` completed with a schema-valid report (CN_EAST 2025-03-31..2025-04-07: 57400.00 vs 70000.00, -12600.00, -0.1800). Persisted events are contiguous seq 1-7 with four ordered `step.completed` rows for `plan_metrics:0`, `execute_metrics:0`, `compose_analysis:0`, `validate_report:0`.
- Negative paths: two concurrent claims produce one attempt; heartbeat renewal observed during a blocked model call; expired/fenced confirmation rejected with no step/event; invalid JSON exhausts three bounded attempts then fails `MODEL_OUTPUT_INVALID` with a failed step and no evidence/completion; unbound tool and invalid arguments fail closed with denied `tool.invocation` audit; missing and foreign evidence fail `validate_report`; no Task tuple lock or idle-in-transaction session is observed while the model is blocked.
- Status left `claimed`; integration verification and resolution remain with the merger.
