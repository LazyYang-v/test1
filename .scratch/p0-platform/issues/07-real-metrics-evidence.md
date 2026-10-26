# 07: 真实指标执行——只读 SQL、Evidence、变化率

**What to build:** execute_metrics 从桩换成真实路径：经 Tool Gateway 以模板化参数化只读 SQL 查询授权视图（business_ro 角色），区域过滤由平台强制注入；每次查询产出不可变 Evidence（查询计划、结果集哈希、行数、data_snapshot_id）；变化率由确定性计算器产生。首个业务用例（华东 7 天 GMV 环比）端到端真实闭环。

**Blocked by:** 06

**Status:** claimed

- [x] GMV 查询经只读角色执行：参数化绑定、区域强制注入、授权区域混合数据下只返回授权区域（BA-08）
- [x] 每次查询产出 Evidence 并持久化：查询计划（脱敏）、结果集哈希、行数、快照 ID；同一 Task 内全部证据快照一致
- [x] 变化率由确定性计算器产生：下降 18% → "-0.1800"；上期为零 → null + ZERO_BASELINE，无除零（BA-01/02/03）
- [x] 本期无订单 → 零值 + 数据不足说明（BA-04）；跨月跨时区边界正确（BA-05）
- [x] 报告数值与 Evidence 相符：validate_report 校验引用存在、归属当前 Task、数值一致（BA-10）
- [x] 工具调用唯一约束 (task_id, invocation_key) 生效；同请求重复执行复用持久化结果
- [x] 模型节点（Fake）不能影响数值：计划只决定查询什么，数值全部来自平台计算

## Comments

2026-10-26: Implementation started on `ticket/07-real-metrics-evidence` from `integration/p0` tip `1cf9f50`. Scope: real read-only GMV template/compiler, deterministic Decimal delta, Evidence query plan/result hash/row count/snapshot, `AGENTFORGE_BUSINESS_RO_DB_DSN` wiring, and the HTTP -> worker -> PostgreSQL seam over the canonical frozen dataset. Status claimed; resolution remains with the merger.

### Verification (2026-10-26)

- Branch/commits: `ticket/07-real-metrics-evidence`; feature commit `65a4438`. `integration/p0` tip `1cf9f50` merged (already up to date). No new migration; migrations remain 0003-0008 only, `SCHEMA_VERSION` stays 8, and the worker role's business grants are unchanged.
- Full suite after merge: `uv run pytest -q --tb=short` -> `376 passed in 428.29s (0:07:08)`, exit code 0, continuous 0001-0008 sequence with real Docker PostgreSQL.
- Dataset: `tests/integration/worker_support.py` now loads the canonical frozen dataset through `write_dataset` + `load_dataset`; the ticket-06 state-only completion fixture is gone.
- Endpoint/worker evidence: `POST /api/v1/tasks` -> real worker -> `GET /api/v1/tasks/{id}` completed. CN_EAST 2025-03-31..2025-04-07 vs prior 7 days: `57400.00` / `70000.00` / `-12600.00` / `-0.1800`, evidence `row_count=1400`, `data_snapshot_id=e38d...b5b8`, 64-hex `result_set_hash` equal to the canonical result hash, and a redacted `query_plan_json` naming `business.gmv_v1`, `sum(paid_amount)`, region `["CN_EAST"]`, and the half-open UTC instants. Report metrics equal the platform Evidence and the finding cites that evidence id.
- BA-08: with CN_NORTH/CN_SOUTH rows present in the snapshot, a CN_EAST-scoped Task returns only `57400.00` and the persisted plan lists only `["CN_EAST"]`. A plan asking for `["CN_EAST", "CN_NORTH"]` fails `REGION_ACCESS_DENIED` before SQL with a denied `tool.invocation` audit and no Evidence. A non-registered dimension fails `QUERY_PLAN_INVALID`.
- BA-03/04: current 2025-01-15..2025-01-22 (previous is the zero window) -> `56000.00` vs `0.00`, `change_rate=null`, `ZERO_BASELINE` annotation and report limitation. Current 2025-01-08..2025-01-15 -> `0.00`, `row_count=0`, nonzero previous, `change_rate=-1.0000`, `DATA_INSUFFICIENT` annotation and report limitation; no division by zero.
- BA-05: an `Asia/Shanghai` local 2025-01-31 window compiles to `2025-01-30T16:00Z..2025-01-31T16:00Z`; current/previous values and row count match an independent `agentforge_business_ro` query. The 12.34 fixture at 15:30Z is in the current period; the 23.45 fixture at 16:30Z is excluded (local Feb 1).
- Replay: two identical `ToolGateway.invoke` calls with the same `(task_id, invocation_key)` return `reused=False` then `reused=True` with identical result/evidence id; a counting executor records one execution, and the database has one succeeded tool invocation and one Evidence row.
- Deviations: `harness.evidences` has no annotations column and 0008 is immutable, so `ZERO_BASELINE`/`DATA_INSUFFICIENT` live in the platform-owned `query_plan_json`; `build_report` deterministically turns them into report limitations. The focused GMV template validates `dimensions` against the registered schema but returns one authorized-scope total (per-dimension grouping stays in ticket 10).
- Status left `claimed`; integration verification and resolution remain with the merger.
