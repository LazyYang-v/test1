# 12: 评测 Runner

**What to build:** 固定 Benchmark 经公开 API 驱动真实任务并产出确定性结论：数据集与案例落库（dataset 绑定 data_snapshot_id）、评测任务统一归属 svc-eval 服务账号（仅 fixture 区域只读）、发起时校验快照一致、案例结果含确定性断言（数值、变化率、证据引用结构）。默认 Fake Model，Live 模式显式开启且受调用预算约束。

**Blocked by:** 07

**Status:** ready-for-human

- [x] datasets/cases/runs/case_results 落库；dataset 显式绑定 data_snapshot_id
- [x] `POST /evaluations/runs` 发起评测；评测任务 owner 为 svc-eval，不出现在普通用户任务列表
- [x] 发起时校验任务快照 == dataset 快照，不一致该 case 直接判失败
- [x] 10 个核心案例（BA-01~10）确定性断言全部通过；结果含 status/score/failure_codes/duration
- [x] 「模型虚构原因」类案例以结构断言覆盖：hypotheses 不得冒充 findings、findings 必须引用有效证据
- [x] 默认 Fake Model；Live 模式需显式开关 + 调用预算，超预算立即停止
- [x] 评测不改写任务原始结果，只读取与判定

## Comments

- 2026-10-27 (orchestrator): ticket branch merged into `integration/p0` at `3c060ec`, with the migration-0011/wiring repair `6344216` (fixes malformed seed UUIDs, binds eval tasks to runs via `eval_run_id`/`trace_id`, grants API/worker roles evaluation DML, threads `fake_model`/`live_model` selection through the Worker; offline tier 181 passed, all 12 migrations parse under libpg_query). Post-merge integration-tier verification is pending: the current environment cannot run Docker/PostgreSQL, so the ticket is handed to a human/CI environment to execute `uv run pytest tests/integration -q` and confirm the acceptance boxes above. See `docs/verification/release-gate-evidence.md`.