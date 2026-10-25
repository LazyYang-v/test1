# AgentForge P0 — 可靠执行底座与首个经营分析闭环

Status: ready-for-agent
Spec 版本: 1.0（2026-10-26）
设计依据: `docs/architecture/p0-design-baseline.md`（v1.1，唯一设计真相源）；`docs/adr/0001–0011`；`GLOSSARY.md`

## Problem Statement

电商业务的分析问题（如「华东区域最近 7 天 GMV 为什么变化」）目前没有一条受控、可靠、可审计的自动化路径：要么人工写 SQL（慢、口径漂移、越权风险），要么直接调 LLM（不可复现、无法核验、故障后状态不明）。需要一个执行底座，让真实模型驱动的经营分析任务在授权边界内运行、产生可核验结果、经历故障后安全恢复、全程可测试可审计——而这一切在 P0 之前完全不存在，仓库是空的。

## Solution

建成 AgentForge P0：用户通过 API 提交经营分析任务，平台以 Task/Attempt/租约/Fencing 管理其生命周期，Worker（asyncio）执行线性四节点图（plan_metrics → execute_metrics → compose_analysis → validate_report），模型只产出指标查询计划、由平台语义层编译为参数化只读 SQL 执行，数值与变化率全部由确定性组件产生并落为不可变 Evidence，报告经确定性校验后持久化为任务终态；Worker 崩溃后由 step_executions 重放-记忆化恢复；事件经 SSE 推送、断线可补发；固定 Benchmark 在冻结数据快照上确定性回归。全程留有八类审计事件与 Trace ID。

## User Stories

### M1 — 数据库、迁移、Identity、Registry

1. 作为平台用户，我希望用邮箱密码登录换取短时效 JWT（≤15 分钟），以便以我的身份安全调用 API。
2. 作为平台用户，我希望只有被授予的区域权限内的数据对我可见可查，以便数据范围始终受控。
3. 作为企业管理员，我希望权限撤销立即生效（敏感操作实时校验、不信任 JWT 中的旧权限），以便权限回收无延迟窗口。
4. 作为开发者，我希望迁移有序、可重复执行且服务启动前校验 Schema 版本，以便服务永远不会带着错误 Schema 进入 Ready。
5. 作为运维，我希望迁移失败阻止服务就绪，以便半迁移状态不会被流量触及。
6. 作为 Agent 作者，我希望注册并发布 Agent 版本为不可变配置（含冻结的模型配置快照），以便历史任务行为永远可复现。
7. 作为 Agent 作者，我希望退役只影响新任务、不影响已创建任务，以便在线任务不被中断。
8. 作为运维，我希望数据库角色按进程隔离（迁移/API/Worker/只读业务/评测），以便任一进程被攻破都无法读写全部数据。
9. 作为审计人员，我希望权限授予/撤销与 Agent 发布/退役都产生追加式审计事件，以便安全与合规动作可追溯。

### M2 — Task、Attempt、租约、幂等、事件

10. 作为 API 客户端，我希望携带 Idempotency-Key 创建任务并得到 202，以便网络重试不会创建重复任务。
11. 作为 API 客户端，我希望同键同请求重放同一响应、同键异请求得到 409，以便幂等语义可以被依赖。
12. 作为 API 客户端，我希望按用户、状态、日期分页列出任务，以便追踪我的工作。
13. 作为 API 客户端，我希望查询任务详情（状态、版本、当前 Attempt、进度、结果、错误），以便向最终用户呈现。
14. 作为平台用户，我希望取消任务（携带乐观版本），以便及时止损；未领取的直接取消，运行中的协作式取消。
15. 作为平台用户，我希望任务终态不可逆转，以便已完成/失败/取消的结果不被后续事件污染。
16. 作为 API 客户端，我希望任务事件在事务内分配任务内递增序号且持久化早于通知，以便 SSE 补发与去重有据可依。
17. 作为 API 客户端，我希望所有错误都是统一结构（code/message/retryable/request_id），以便程序化处理失败。

### M3 — Worker 与恢复协议

18. 作为平台用户，我希望 Worker 崩溃后任务自动恢复或明确失败并保留诊断，以便无需人工介入也不出现伪造成功。
19. 作为平台用户，我希望已确认步骤的结果在恢复时被复用而不重跑，以便恢复既便宜又幂等。
20. 作为开发者，我希望两个 Worker 并发领取只有一个有效租约，以便任务不被重复执行。
21. 作为开发者，我希望旧执行者的一切提交被 Fencing 拒绝，以便僵尸 Worker 无法污染任务状态。
22. 作为平台用户，我希望可重试错误按退避重试、不可重试错误明确失败（按冻结的错误分类表），以便任务结局始终符合语义。
23. 作为平台用户，我希望超过 Deadline 的任务被终止，以便任务不会无限占用资源。
24. 作为运维，我希望取消与完成并发竞争只产生一个合法终态，以便终态永远可信。

### M4 — Harness、Model Adapter、Tool Gateway

25. 作为 Agent 作者，我希望上下文按固定顺序组装、超预算时只裁剪低优先级历史而不裁剪安全策略与关键证据，以便模型行为可控。
26. 作为平台开发者，我希望模型输出经 JSON Schema 校验、有限次修复重试、超预算即失败而非静默默认结论，以便输出可靠。
27. 作为平台开发者，我希望 DeepSeek 与 Deterministic Fake Model 经同一适配接口可配置互换，以便离线确定性测试与真实验收走同一代码路径。
28. 作为平台用户，我希望调用未绑定给 Agent 版本的工具被拒绝，以便 Agent 权限边界严格。
29. 作为平台用户，我希望每次工具调用都经过九步网关（Schema、实时权限、绑定与预算、租约与取消、幂等 invocation、审计与 Trace），以便每次数据访问受控且可追溯。
30. 作为安全工程师，我希望任务运行中撤销用户权限后、后续工具调用立即被拒，以便权限即时生效。
31. 作为 Agent 作者，我希望 Token/工具次数/成本预算被强制执行（BUDGET_EXCEEDED 不可重试），以便成本有硬上限。

### M5 — 语义层、SQL 编译器、业务闭环

32. 作为平台用户，我希望提问「华东最近 7 天 GMV 环比」并获得结构化指标、变化率与可核验的分析说明，以便完成首个业务用例的完整闭环。
33. 作为平台开发者，我希望模型只能产出指标查询计划、由平台编译为参数化只读 SQL，以便系统不存在自由 SQL 路径。
34. 作为安全工程师，我希望注入值被参数化绑定、多语句/DDL/DML 被编译器 AST 拒绝、执行角色无写权限、区域过滤由平台强制注入，以便具备纵深防御。
35. 作为平台用户，我希望变化率由确定性计算器产生、上一周期为零时返回 ZERO_BASELINE 而非除零，以便数值永不依赖模型算术。
36. 作为平台用户，我希望跨月、跨时区、左闭右开日期边界被正确处理，以便口径一致无边界错误。
37. 作为平台用户，我希望未支付订单不计入 GMV、已退款订单仍按冻结口径计入，以便指标符合已冻结的业务定义。
38. 作为平台用户，我希望区域混合数据下只返回我授权区域的数据，以便不发生跨区域泄漏。
39. 作为平台用户，我希望报告 findings 必须引用有效 Evidence、hypotheses 必须标注待验证而不冒充事实，以便结论可核验、虚构可识别。

### M6 — 评测、SSE、可观测性、部署验收

40. 作为 API 客户端，我希望经 SSE 实时订阅任务事件、断线后凭 Last-Event-ID 补发，以便实时且不丢事件。
41. 作为 API 客户端，我希望 NOTIFY 丢失、监听中断或 API 重启后事件仍可从持久化存储读取补发，以便 at-least-once 语义可依赖。
42. 作为评测工程师，我希望固定 Benchmark 在固定数据快照上跑确定性断言（数值、变化率、证据引用结构），以便得到可靠的回归信号。
43. 作为评测工程师，我希望评测任务归属专用服务账号、不混入业务任务列表、Live 模式显式开启且受调用预算约束，以便测试隔离且不静默烧钱。
44. 作为运维，我希望每个 Task 可关联 Trace ID、模型与工具有结构化调用记录，以便失败能定位到步骤与错误类别。
45. 作为运维，我希望 Docker Compose 一键冷启动（含迁移与种子）且健康检查通过，以便验收与演示可重复执行。
46. 作为 API 客户端，我希望触发限流时收到 429 与 Retry-After，以便礼貌退避重试。
47. 作为演示者，我希望 Web 界面能提交任务并展示状态、事件与最终报告，以便核心流程对非技术干系人可演示。

## Implementation Decisions

全部细节以设计基线 v1.1 为准（含完整表结构、API 示例、状态转移表、崩溃窗口矩阵）；此处记录决定本身及其 ADR 出处：

- **单一持久化真相源**：PostgreSQL 承载 Task 生命周期与恢复；LangGraph 在 P0 仅作进程内编排器（MemorySaver），不承担持久化（ADR-0011，修订 ADR-002、吸收 ADR-0007）。
- **恢复协议 = 重放-记忆化**：新 Attempt 从头执行四节点图，每节点先查 step_executions——已确认则复用结果、未记录则执行、无法确认恢复状态则明确失败并保留诊断。步骤键为稳定逻辑节点路径+迭代号，不含 Attempt ID。
- **租约 + Fencing + Attempt 隔离**（ADR-0008）：每次领取递增 fencing epoch；Worker 的一切写入（终态、事件、步骤确认、工具结果接纳）必须在同一事务内校验 (lease_token, fencing_epoch)。
- **自研薄层 Harness**（ADR-0001）：ContextBuilder / ModelAdapter / ToolRegistry / ToolGateway / PolicyEngine / ExecutionBudget 均为 Python Protocol；Runtime 生成的 ExecutionContext 中身份、区域、租约、预算对模型不可修改。
- **P0 最小图**：plan_metrics（模型）→ execute_metrics（确定性，经 Tool Gateway）→ compose_analysis（模型）→ validate_report（确定性校验 Schema、Evidence 引用、数值、快照一致）。
- **语义层 + 平台 SQL 编译器**（ADR-0004）：模型唯一数据访问表达是指标查询计划；只接受注册指标/维度，过滤值全部参数化，AST 拒绝多语句/DDL/DML，只读执行角色，区域过滤平台强制注入。
- **Evidence 与数据快照**（ADR-0009）：每次受控查询的不可变结果记录为 Evidence（平台生成、模型只可引用）；Task 创建时绑定种子数据集内容哈希 data_snapshot_id，同任务全部证据同快照。
- **GMV 冻结口径**：paid_at ∈ [start, end) 的订单 paid_amount 合计；订单状态 pending/paid/refunded/cancelled；退款不扣除，净收入为 P1 独立指标。
- **认证**：短时效 JWT（HS256，≤15 分钟）+ 敏感工具执行前实时查询区域授权；无 refresh token、无会话表。
- **事件与通知**：task_events 事务内递增序号、追加不可改、永久保留；事务内 pg_notify 唤醒 + 10 秒兜底轮询；SSE 以 task_event_seq 为事件 ID，支持 Last-Event-ID 补发；无 Outbox、无 Redis 必需依赖。
- **错误分类**：可重试（provider 瞬时/超时/限流）→ retry_wait（next_retry_at，30s+抖动）；不可重试（Schema 耗尽/计划修复后仍非法/权限拒绝/预算/Deadline/不变量违反）→ failed；所有权失效 → attempt abandoned 后由状态机裁决。
- **Worker 模型**：asyncio 单进程多并发（asyncpg + httpx + LangGraph 异步 API）；恢复扫描为 Worker 内协程（SKIP LOCKED），不设独立 recovery 服务。
- **Registry 管理**：无管理 API；迁移种子 + 仓库 CLI；发布不可变性由数据库触发器/权限与服务层双重保证；模型配置在发布时冻结为快照，运行时不读可变 profile。
- **数据库**：单实例多 Schema（identity/registry/runtime/harness/business/evaluation/audit），十一个有序迁移；数据库角色按进程隔离；审计八类动作追加写入。
- **评测隔离**：评测任务统一归属 svc-eval 服务账号（仅 fixture 区域只读）；dataset 绑定快照并在发起时校验一致；默认 Fake Model，Live 显式开关+预算。
- **部署**（ADR-0006）：Compose 必需服务 web/api（单副本）/worker/postgres/migrate/seed；Tempo/Prometheus/Grafana 与 Redis 为可选 profile；限流为进程内按用户令牌桶。
- **模型侧已知约束**（附录 A）：DeepSeek Chat Completions 仅支持 json_object、无 json_schema 严格模式；Prompt 须含 "json" 字样与示例；须处理空 content 与 finish_reason=length 截断；Schema 强校验由平台校验器承担。

## Testing Decisions

- **好测试只测外部行为**：断言对象是 API 响应、任务状态/终态、事件序列、结果 JSON、错误码与审计记录；不断言内部函数调用、私有结构或实现细节。
- **唯一的自动化主缝是 HTTP API（含 SSE）**：RT/HS/BA 全部测试矩阵从 /api/v1 进入，背后是真实 PostgreSQL + Deterministic Fake Model。无更高缝可用（这就是系统边界）。
- **模型替换点**：ModelAdapter 接口上配置 Fake Model（可脚本化输出，含恶意查询计划），使主缝确定性；DeepSeek 与 Fake 走同一适配代码路径。
- **唯一测试专用缝：故障注入钩子**，精确落在基线 §6.4 的八个崩溃窗口（创建事务前后、领取后、模型期间/返回后、工具落库前后、步骤确认后、终态前、通知前），由测试精确触发，禁止随机杀进程。验收要求证明重放恢复不会产生错误的成功状态。
- **三环境**：Offline（Fake + 固定快照，快速确定性）/ Integration（Fake + 真实 PostgreSQL，事务并发恢复）/ Live（DeepSeek + 固定快照，≥3 次端到端留证据；不进自动化回归）。
- **Web 前端不设自动化测试缝**：演示级交付，随 M6 人工验收。
- **无先例可循**：仓库为绿地，本 spec 的测试将建立项目测试惯例；测试矩阵以基线 §14 的 RT-01~15、HS-01~12、BA-01~10 为验收清单。
- **里程碑即测试门槛**：M1–M6 各自的完成判据是对应测试通过；不为赶工期跳过测试。

## Out of Scope

以下均属 P1+，不得在本 spec 实施中出现（详见基线 §16）：

- 持久化 LangGraph Checkpointer 及其并发写入隔离、分支恢复、fencing 适配层（P1 强制前置，须先完成选型验证）
- 写路径工具与副作用对账协议（unknown 先对账再重试）
- Outbox 与跨进程/跨服务通知（webhook 等）、Redis 必需化
- 多副本限流与全局速率控制、事件保留期与归档清理
- Registry 管理 API 与完整 RBAC、审批流、沙箱
- 退款净收入指标、生产级数据快照（时点查询）
- Supervisor + 六专业 Agent（ADR-003 目标架构）、Memory/Skills/子 Agent、HITL
- LLM Judge 与评测控制台、Agent Studio、Kubernetes/Kafka/Elasticsearch/向量数据库
- 任务克隆/重跑操作（P0 重跑 = 新建 Task）

## Further Notes

- 设计基线 v1.1 与 11 份 ADR 是实现冲突时的裁决依据；术语以 GLOSSARY.md 为准（Evidence、Step Execution、Data Snapshot、Metric Query Plan 等）。
- 实施约束：1 人全职 + LLM 结对；时间盒 6–8 周；M1→M6 串行验收；Runtime 状态机与恢复协议不得并行修改。
- 种子数据集必须确定性生成（固定随机种子、生成器版本、哈希算法、排序规则），并内嵌 BA-01~08 全部 fixture（含退款单与零基线窗口）。
- 最终验收以运行证据为准（测试报告、崩溃恢复记录、权限拒绝记录、真实模型调用记录、Trace 导出、Compose 健康检查）；「设计完成」「代码已写」不构成验收。
