# AgentForge P0 设计基线 v1.1

- **状态**：决策冻结（三轮设计盘问完成，2026-10-26），待 M1 实施
- **真相源**：本文档是 P0 唯一设计真相源。术语见根目录 `GLOSSARY.md`；架构决策见 `docs/adr/`（0001–0011）
- **实施约束**：1 人全职 + LLM 结对；时间盒 6–8 周（计划，非工期承诺——M3 恢复协议或 M5 SQL 编译器未达验收时不得为赶工期跳过测试）
- **交付方式**：先设计与验收契约，M1 起编码

---

## 0. 变更记录（v1.0 → v1.1）

| # | 变更 | 来源 |
|---|------|------|
| 1 | 定位冻结：平台优先，GMV 用例为驱动场景；1 人 + 6–8 周 | R1-Q1 |
| 2 | Tempo/Prometheus/Grafana 降为可选；Recovery 扫描并入 Worker，删独立 recovery 服务 | R1-Q2 |
| 3 | 新增 `harness.evidences`；Evidence 由平台生成、不可变 | R1-Q3 |
| 4 | 冻结订单状态机与 GMV 谓词；退款字段建模推迟 P1 | R1-Q4 |
| 5 | 新增 `runtime.tasks.data_snapshot_id`（种子内容哈希绑定 Task） | R1-Q5 |
| 6 | SSE 唤醒 = 事务内 pg_notify + 10s 兜底轮询；Redis 移出 P0 必需 | R1-Q6 |
| 7 | 认证冻结：短时效 JWT HS256 ≤15min + 敏感操作实时鉴权 | R1-Q7 |
| 8 | 新增 `runtime.tasks.next_retry_at` + 部分索引 | R1-Q8 |
| 9 | Worker 并发模型冻结：asyncio 单进程多并发、协作式取消 | R1-Q9 |
| 10 | Agent 发布时冻结模型配置快照 | R1-Q10 |
| 11 | P0 最小图冻结：四节点线性图，无分支无循环 | R1-Q11 |
| 12 | **P0 不使用持久化 LangGraph Checkpointer**：MemorySaver + step_executions 重放恢复（ADR-0011，修订 ADR-002） | R2-Q1 |
| 13 | 幂等并发窗口收口；只读工具 unknown 可安全重试 | R2-Q2 |
| 14 | 评测隔离：svc-eval 服务账号 + 快照校验 + Live 显式开关 | R2-Q3 |
| 15 | 错误分类与重试语义表冻结 | R2-Q4 |
| 16 | 种子数据集冻结：确定性生成、内嵌 fixture | R2-Q5 |
| 17 | API 限流：单副本进程内令牌桶 | R2-Q6 |
| 18 | 删除 `runtime.outbox_events` 与 Outbox 发布器；RT-14 改写 | R3-Q2 |
| 19 | task_events 永久保留、追加不可改、内容脱敏 | R3-Q3 |
| 20 | Registry 无管理 API：迁移种子 + CLI；发布不可变性由 DB 约束保证 | R3-Q4 |
| 21 | 八类审计动作冻结 | R3-Q5 |
| 22 | M3 更名为 step_executions 恢复协议；时间盒 6–8 周 | R3-Q6 |
| 23 | 本文件入库，替代对话版 v1.0 | R3-Q1 |
| 24 | 附录 A：LangGraph / DeepSeek 事实研究结论入库（P1 选型输入；实证支持 ADR-0011） | 研究员 |

---

## 1. 定位与成功标准

P0 不以「做出六个 Agent」为目标，而是建立能承载它们的可靠执行基础。P0 结束时：用户通过 API 提交经营分析任务，真实模型驱动最小分析流程，执行授权指标查询，返回结构化结果；Worker 崩溃后能恢复或明确终止任务，全程留有审计记录。

**首个业务用例**：华东区域最近 7 天 GMV 与前 7 天比较，计算变化率，返回结构化指标与可核验的分析说明。覆盖：身份与区域权限检查 → Task 创建与幂等 → Worker 异步执行 → 模型生成结构化查询计划 → 平台编译执行只读 SQL → 确定性计算变化率 → 模型生成分析说明 → Schema 与证据核验 → 终态持久化 → SSE 通知 → 失败重试与恢复。

**P0 禁止**：模型执行任意 SQL、Shell、Python；修改经营数据。

| 能力 | P0 | 后续 |
|------|----|----|
| Harness | Context/Model/Tool/Policy 最小实现 | Memory、Skills、子 Agent |
| Runtime | Task、Attempt、租约、恢复、取消、SSE | 高级调度、HITL |
| 图 | 线性四节点 + step_executions 恢复 | Supervisor + 6 Agent（需持久化 Checkpointer，见 §16） |
| 数据查询 | 白名单指标、参数化 SQL 编译 | 丰富指标与多维诊断 |
| Evaluation | 固定案例、确定性断言 | LLM Judge、评测控制台 |
| Observability | 结构化日志、Trace ID、OTel SDK 就绪 | 完整链路分析与成本看板 |
| 前端 | 任务提交、状态、事件、结果 | Agent Studio |
| 安全 | 认证、数据范围、只读执行 | 完整 RBAC、审批、沙箱 |

## 2. 架构与模块边界

```
React Web ── API Client（任务提交 · 详情 · SSE · 结果）
FastAPI Control Plane（Auth · Task Service · Idempotency · Query API · Event Stream）
PostgreSQL（Tasks · Attempts · StepExecutions · Events · 幂等 · Invocations · Evidences）
Runtime Worker（Scheduler · Executor · Heartbeat · Recovery 扫描 · asyncio 并发）
LangGraph + Harness（Context Builder · Model Adapter · Tool Gateway；MemorySaver 仅进程内）
LLM Provider（DeepSeek · Deterministic Fake Model · 可替换适配器）
Metrics Gateway（语义层 · SQL Compiler · 只读执行）
```

核心原则：**PostgreSQL 是 Task 生命周期与恢复的真相源**；LangGraph 在 P0 只是进程内编排器，不承担持久化职责（ADR-0011）。

| 模块 | 允许负责 | 禁止负责 |
|------|---------|---------|
| API | 校验、授权、创建任务、返回结果 | 长时间 Agent 推理 |
| Task Service | 状态转移、幂等、事件事务 | 调用 LLM |
| Worker | 领取任务、租约心跳、执行图、恢复扫描 | 绕过状态机 |
| Harness | 上下文、模型与工具适配 | 修改任务状态表 |
| LangGraph | 节点编排（进程内） | 充当任务真相源 |
| Tool Gateway | 权限、Schema、预算、审计 | 接受自由 SQL |
| Metrics | 指标语义、SQL 编译、结果计算 | 信任模型产出的数值 |
| Evaluation | 验证冻结的输入输出 | 改写任务结果 |

## 3. 数据库设计

单 PostgreSQL 17 实例、单库、多 Schema：`identity`、`registry`、`runtime`、`harness`、`business`、`evaluation`、`audit`。（v1.0 的 `langgraph` schema 移除——P0 无持久化 Checkpointer。）

**字段约定**：主键 UUID（应用层 UUIDv7）；时间 TIMESTAMPTZ/UTC；金额 NUMERIC(18,2) 禁浮点；计数 BIGINT；状态 VARCHAR + CHECK；元数据 JSONB 但核心状态/关联用显式字段；记录带 created_at，可变记录带 updated_at。

### 3.1 identity

- **users**：id PK · email UNIQUE 规范化 · password_hash 可空 · display_name · status(active/disabled) · token_version 默认1 · created_at · updated_at
- **region_grants**：id PK · user_id FK · region_code · permission(read/admin) · created_at · revoked_at 可空；唯一活跃索引 `(user_id, region_code, permission) WHERE revoked_at IS NULL`

### 3.2 registry

- **agent_versions**：id PK · agent_key · version（唯一 agent_key+version）· name · graph_key · model_profile_id FK（仅溯源）· **model_config_snapshot JSONB NOT NULL**（发布时冻结的生效模型配置，R1-Q10）· config_json · config_hash CHAR(64) · status(draft/published/retired) · created_by · published_at · created_at
  - **发布不可变性由 DB 层保证**：触发器阻止 `status='published'` 行的关键配置列（config_json、graph_key、model_config_snapshot、模型绑定、工具权限）被 UPDATE；CLI 不允许绕过（R3-Q4）
- **model_profiles**：id PK · profile_key UNIQUE · provider · model_name · endpoint_alias · credential_ref（仅密钥引用，无明文）· timeout_ms 正 · max_output_tokens 正 · config_json · created_at（可变行；对运行时的影响经发布快照隔离）
- **tool_versions**：id PK · tool_key+version 唯一 · input_schema · output_schema · policy_json · execution_kind(internal/mcp) · is_read_only（P0 恒 true）· timeout_ms 正 · status · created_at
- **agent_tool_bindings**：agent_version_id + tool_version_id 联合 PK · policy_override 默认 {}（只允许收紧、禁止放宽）

### 3.3 runtime

- **tasks**：id PK · owner_user_id FK · agent_version_id FK · task_type · status（状态机 CHECK）· input_json · input_hash CHAR(64) · region_scope_json · authorization_snapshot · **data_snapshot_id CHAR(64) NOT NULL**（R1-Q5）· priority 默认5 · current_attempt_no 默认0 · max_attempts 默认3 · deadline_at 可空 · **next_retry_at 可空**（R1-Q8）· cancel_requested_at 可空 · completed_at 可空 · result_json 可空 · error_code 可空 · error_message 脱敏 · created_at · updated_at · version BIGINT
  - 索引：`(status, priority DESC, created_at)` 领取；`(owner_user_id, created_at DESC)`；`(agent_version_id, created_at DESC)`；部分索引 `(next_retry_at) WHERE status='retry_wait'`
  - authorization_snapshot 仅审计与恢复一致性用途，非永久授权；敏感工具执行前实时查 region_grants
- **task_attempts**：id PK · task_id FK · attempt_no（唯一 task_id+attempt_no）· status(running/succeeded/failed/abandoned) · worker_id · lease_token UUID · fencing_epoch BIGINT · lease_expires_at · heartbeat_at · started_at · finished_at 可空 · error_code · error_detail 脱敏
- **step_executions**（恢复协议承重表，ADR-0011）：id PK · task_id FK · step_key VARCHAR(160)（稳定逻辑节点路径+迭代号，如 `plan_metrics:0`，**不含 attempt id**）· graph_node_key · input_hash · status(pending/confirmed/failed) · result_ref 可空 · result_hash 可空 · owning_attempt_id FK · fencing_epoch · started_at · completed_at 可空；唯一 `(task_id, step_key)`
  - 已 confirmed 的结果只能被后续 Attempt 引用，不能覆盖
- **task_events**：id BIGINT PK 库生成 · task_id FK · attempt_id 可空 · task_event_seq（唯一 task_id+seq；锁定 Task 行事务内递增分配）· event_type · payload_json（脱敏：无明文凭据、无完整敏感 Prompt、无多余原始业务数据）· occurred_at · trace_id
  - **追加不可改**：业务角色无 UPDATE/DELETE 权限（R3-Q3）；SSE 用 task_event_seq 作事件 ID
- **idempotency_records**：id PK · actor_user_id · operation · idempotency_key（唯一 actor+operation+key）· request_hash CHAR(64) · status(processing/completed) · resource_id · response_status · response_json · expires_at（≥24h）· created_at · updated_at
- **worker_instances**：worker_id PK · instance_id · status(active/draining/stopped) · capacity · active_tasks · started_at · heartbeat_at（运行快照，非租约判据）
- ~~outbox_events~~：**P0 删除**（R3-Q2）

### 3.4 harness

- **model_invocations**：id PK · task_id · attempt_id · invocation_key · agent_version_id · provider · model_name · context_hash · input/output_tokens · estimated_cost NUMERIC(18,8) · status · error_code · trace_id · started_at · finished_at。模型调用 at-least-once，重复成本如实记账
- **tool_invocations**：id PK · task_id · attempt_id · invocation_key（唯一 task_id+invocation_key）· tool_version_id · request_hash · request_json 脱敏 · status(prepared/executing/succeeded/failed/**unknown**) · result_json · result_hash · error_code · started_at · finished_at · trace_id。unknown 在 P0 只读工具下可安全重试；旧 Attempt 的结果不能直接当作新 Attempt 有效结果，须现租约 + fencing 校验接纳（R2-Q2）
- **context_snapshots**：id PK · task_id · attempt_id · graph_node_key · context_hash · context_manifest · estimated_tokens · compression_version 可空 · created_at
- **evidences**（R1-Q3 新增）：id PK（即 evidence_id）· task_id · attempt_id · tool_invocation_id · query_plan_json 脱敏 · result_set_hash CHAR(64) · row_count · data_snapshot_id · created_at。**Evidence = 一次受控指标查询的不可变结果记录，平台生成，模型只可引用**

### 3.5 business

- **orders**：id PK · order_no UNIQUE · region_code · customer_id · ordered_at · status CHECK IN (`pending`/`paid`/`refunded`/`cancelled`) · gross_amount · discount_amount · paid_amount NUMERIC(18,2) · currency CHAR(3) · paid_at TIMESTAMPTZ（仅支付成功时写入，退款后保留不变）· created_at
  - **GMV 谓词**：`paid_at ∈ [start, end)`，金额 SUM(paid_amount)。「曾支付」= paid_at IS NOT NULL ⇔ status ∈ {paid, refunded}；退款不扣除（净收入为 P1 独立指标）
- **order_items**：id PK · order_id FK · sku_id · category_code · quantity · unit_price · discount_amount · paid_amount
- **metric_definitions**：id PK · metric_key+version 唯一 · name · source_view · expression_key · allowed_dimensions · allowed_filters · aggregation · unit · time_column · status · created_at
  - P0 冻结 GMV 定义：指定期间内成功支付订单的实付金额合计，不扣除后续退款，按支付成功时间（paid_at）归集

### 3.6 evaluation 与 audit

- **datasets**：id · dataset_key · version · name · description · content_hash · **data_snapshot_id CHAR(64)**（R2-Q3）· status · created_at
- **cases**：id · dataset_id · case_key · input_json · expected_json · assertions_json · data_snapshot_ref · created_at
- **runs**：id · dataset_id · agent_version_id · status · started_at · finished_at · summary_json
- **case_results**：id · eval_run_id · case_id · task_id · status · score_json · failure_codes · duration_ms · created_at
- **audit_events**：id · actor_type · actor_id · action · resource_type · resource_id · decision · reason_code · metadata_json · trace_id · occurred_at；追加写入，业务账号无 UPDATE/DELETE

### 3.7 迁移顺序

| 迁移 | 内容 | 前置 |
|-----|------|-----|
| 0001 | Schema、数据库角色、扩展 | 无 |
| 0002 | identity | 0001 |
| 0003 | registry（含发布不可变触发器） | 0002 |
| 0004 | runtime：tasks、attempts、step_executions、workers | 0003 |
| 0005 | runtime：task_events、idempotency（**无 outbox**） | 0004 |
| 0006 | harness：调用、上下文、evidences | 0005 |
| 0007 | business | 0001 |
| 0008 | evaluation | 0004、0007 |
| 0009 | audit | 0002 |
| 0010 | 索引、约束、种子（registry 种子、svc-eval 账号、业务数据集） | 前述 |

迁移纪律：生产启动不自动执行破坏性迁移；API/Worker 启动前验证 Schema 版本；迁移失败阻止 Ready；不修改已发布迁移文件；数据迁移兼容性优先。~~LangGraph Checkpointer 单独初始化~~：P0 不需要。

**数据库角色**：agentforge_migrator（DDL）、agentforge_api（平台表）、agentforge_worker（runtime+harness）、agentforge_business_ro（仅业务视图 SELECT）、agentforge_eval。生产服务禁用超级用户；业务数据只经视图暴露给 business_ro。

## 4. API 契约（/api/v1）

JSON + ISO 8601 带时区；任务创建 202 异步语义；幂等写请求带 `Idempotency-Key`。统一错误体：`{error: {code, message, retryable, details}, request_id}`。

| HTTP | 错误码 | 处理 |
|------|-------|------|
| 400 | INVALID_REQUEST | 修正请求 |
| 401 | UNAUTHENTICATED | 重新认证 |
| 403 | REGION_ACCESS_DENIED | 禁止执行 |
| 404 | TASK_NOT_FOUND | 不泄露他人任务 |
| 409 | IDEMPOTENCY_CONFLICT | 同键不同哈希才触发 |
| 409 | INVALID_STATE_TRANSITION | 重读状态 |
| 422 | QUERY_PLAN_INVALID | 修正查询计划 |
| 429 | RATE_LIMITED | 按 Retry-After 重试 |
| 503 | MODEL_PROVIDER_UNAVAILABLE | Runtime 分类重试 |

- **POST /tasks**：请求含 agent_key、task_type、input（question、region_codes、左闭右开 date_range、compare_mode）、execution（max_attempts、timeout_seconds）。约束：agent_key 解析到已发布版本；日期范围 ≤90 天；region_codes ⊆ 当前授权区域；超时/重试上限由平台封顶。响应 202 + task_id + links
- **幂等并发窗口**（R2-Q2）：同键同哈希重复请求——首个仍在处理时，第二个撞唯一约束后重读并重放同一响应；409 仅限同键不同哈希
- **GET /tasks/{id}**：状态、version、current_attempt_no、progress（仅展示，非完成判据）、result、error
- **POST /tasks/{id}/cancel**：202；进入 cancelling 或未领取直接 cancelled；协作式取消，靠 fencing 阻止旧执行者污染
- **GET /tasks/{id}/events**（SSE）：id=task_event_seq；支持 Last-Event-ID 补发；事件持久化早于通知；断连不影响执行；客户端按序号去重；at-least-once。`RESYNC_REQUIRED` 错误码保留定义，P0 不会触发（事件永久保留，R3-Q3）
- **指标查询计划 Schema**（模型唯一数据访问表达）：`{metric, metric_version, dimensions, filters, time_range, comparison, limit}`
- **工具返回**：`{metric, metric_version, unit, current_value, previous_value, absolute_change, change_rate, evidence_id, data_snapshot_id}`；change_rate 为小数比例字符串（如 "-0.1800"）；上期为零 → null + ZERO_BASELINE，禁除零。变化率由确定性计算器生成
- **结果 Schema**：summary、metrics、findings（每项引用 ≥1 有效 Evidence）、hypotheses（标注待验证，禁混入已证实事实）、recommendations、limitations、evidence_ids、report_version
- **管理/评测只读 API**：GET agents / versions / tasks / attempts / tool-calls（脱敏）/ evaluations·datasets / runs；POST evaluations/runs；/health/live、/health/ready（Ready 需验证迁移版本与核心依赖，非仅端口连通）
- **限流**（R2-Q6）：单 API 副本、按用户进程内令牌桶（默认容量 10、补充 1/s，可调），429 + Retry-After = 缺口/补充速率向上取整；不宣称多副本全局限流

## 5. Runtime 状态机

状态：queued · running · retry_wait · recovering · cancelling · **completed / failed / cancelled（终态）**。waiting_approval、pausing 保留不启用。

| 当前 | 触发 | 目标 |
|------|------|-----|
| queued | Worker 领取 | running |
| queued | 用户取消 | cancelled |
| running | 全部步骤成功 | completed |
| running | 可重试错误 | retry_wait（写 next_retry_at） |
| running | 不可重试错误 | failed |
| running | 用户取消 | cancelling |
| running | 租约失效 | recovering |
| retry_wait | 到达 next_retry_at | queued |
| retry_wait | 用户取消 | cancelled |
| recovering | 步骤记录可恢复 | queued |
| recovering | 无法安全恢复 | failed |
| recovering | 已请求取消 | cancelled 或 cancelling |
| cancelling | 执行已安全停止 | cancelled |
| cancelling | Worker 失联 | recovering |

终态不可逆转；重跑必须新建 Task。Task Attempt 数、模型调用重试、工具调用重试、整体 Deadline、Token/成本预算分别限额。

**默认参数**：心跳 5s；租约 30s；恢复扫描 10s（Worker 内协程，SKIP LOCKED 认领）；任务最大执行 300s；最大 Attempt 3；模型单次超时 60s；工具单次 15s。均为可调默认值。

## 6. 执行与恢复协议（重放-记忆化）

### 6.1 执行模型

`LangGraph MemorySaver（仅进程内，不作恢复依据）+ PostgreSQL（tasks / attempts / step_executions / task_events / tool_invocations / evidences）`。Worker 重启后必须重建图执行上下文。

### 6.2 Attempt 生命周期

1. **领取**（短事务）：锁 Task 行 → 校验 queued 或 retry_wait 到期 → attempt_no+1、fencing_epoch+1、建 attempt(running)、写租约、Event、Task→running
2. **执行图**：从头执行四节点图；每个节点先查 `step_executions(task_id, step_key)`：已 confirmed → 复用持久化结果（不调模型/工具）；未记录 → 执行
3. **节点内调用**：模型调用经 ModelAdapter（at-least-once，如实记账）；工具经 ToolGateway 九步流程，`tool_invocations(task_id, invocation_key)` 已 succeeded → 复用；unknown → 只读可安全重试
4. **步骤确认**（短事务）：验证 lease+fencing → 写 step_executions(confirmed) → task_event(step.completed)
5. **终态**（短事务）：验证 lease+fencing → Task 终态 + result/error → Event
6. **崩溃恢复**：心跳停 → 租约过期 → Worker 内恢复扫描器（SKIP LOCKED）标旧 attempt abandoned → Task→recovering → 校验步骤记录可恢复 → queued → 新 Attempt 重放；无法确认恢复状态 → failed + 诊断证据，**禁止伪造成功**

### 6.3 不变量

- 一切 Worker 写入携带 (lease_token, fencing_epoch) 并在同一事务内校验；租约失效者不得写终态、事件、步骤确认、工具成功记录（RT-07）
- step_key 稳定（逻辑节点路径 + 迭代号，不含 Attempt ID）
- 数据库事务短小；禁止持 Task 行锁等待模型/工具
- 已确认步骤结果只能引用、不能被覆盖；旧 Attempt 结果须经现租约 + fencing 校验才能接纳
- 外部只读查询执行前与结果接纳前双重租约校验 + DB 侧 statement_timeout

### 6.4 崩溃窗口矩阵

| 窗口 | 策略 |
|------|-----|
| 创建事务提交前 | 客户端重试 |
| 提交后、通知前 | 兜底轮询补发 |
| 领取后、图启动前 | 租约过期重新领取 |
| 模型返回后、落库前 | 重调模型，成本如实记账 |
| 工具成功后、结果落库前 | 只读工具直接重试 |
| 工具结果落库后、步骤确认前 | 重放时按 invocation_key 复用 |
| 步骤确认后、终态前 | 新 Attempt 从已确认步骤续跑 |
| 取消与完成竞争 | Task 行锁 + version 唯一终态 |

## 7. Harness

Python Protocol 设计层接口：ContextBuilder.build / ModelAdapter.generate / ToolRegistry.resolve / ToolGateway.invoke / PolicyEngine.authorize / ExecutionBudget.check。CheckpointManager 接口 P0 不需要（无持久化 Checkpoint）。

**ExecutionContext**（Runtime 生成，模型不可修改）：task_id、attempt_id、agent_version_id、principal_id、region_scope、lease_token、fencing_epoch、trace_id、deadline_at、budget、cancellation_token。

**Context Builder** 组装顺序：平台系统策略 → Agent 版本化指令 → 任务与区域范围 → 当前节点目标 → 已验证工具结果 → 历史摘要 → 输出 JSON Schema。逐片段记录来源/哈希/Token 估算；超预算裁剪低优先级历史，不裁剪安全策略、权限、关键证据。

**Model Adapter**：统一请求响应、JSON Schema 校验、超时与错误分类限流、Token/成本统计、Provider 请求 ID、配置版本冻结（读 model_config_snapshot）。DeepSeek 真实 API + Deterministic Fake Model（离线测试/故障注入）。输出不符 Schema 有限修复重试，超预算必须失败，禁静默默认结论。M4 实现事实（见附录 A）：DeepSeek Chat Completions 仅支持 `response_format: json_object`、无 `json_schema` 严格模式——Prompt 必须含 "json" 字样与示例以防无限空白流；须处理空 content 与 `finish_reason=length` 截断；Schema 强校验由平台校验器承担，可选 Beta strict tool-call 模式。

**P0 最小图**（R1-Q11，step_key = 节点键:迭代号）：
1. `plan_metrics`（模型）— 生成结构化指标查询计划
2. `execute_metrics`（确定性）— Tool Gateway + SQL 编译器执行，产出 Evidence
3. `compose_analysis`（模型）— 基于已验证指标生成分析说明
4. `validate_report`（确定性）— Schema、Evidence 引用、数值、快照核验

**Tool Gateway 九步**：验证工具版本 → 验证输入 Schema → 用户/区域权限（实时）→ Agent 绑定与预算 → 租约与取消状态 → 幂等 ToolInvocation → 执行 → 校验输出并持久化 → 审计与 Trace。

**SQL 编译器**：MetricPlanner → SemanticValidator → QueryCompiler → SQLValidator → ReadOnlyExecutor → ResultNormalizer。只接受注册指标/维度；过滤值全部参数化；表/字段/表达式白名单；区域过滤平台强制注入；限时间范围/行数/查询时长；只读账号无写权限；AST 检查拒绝多语句/DDL/DML/未支持函数；固定 search_path；模型不得指定表名或 SQL 片段。AST 检查是纵深防御，不是自由 SQL 的许可。

## 8. 错误分类与重试语义（R2-Q4）

| 类别 | 错误 | 处置 |
|------|------|-----|
| 可重试 | Provider 5xx / 超时 / 网络瞬断 / 限流（遵守 Retry-After） | → retry_wait，30s + 抖动退避，写 next_retry_at |
| 不可重试 | Schema 修复重试耗尽 / 查询计划修复后仍非法 / REGION_ACCESS_DENIED / BUDGET_EXCEEDED / DEADLINE_EXCEEDED / 内部不变量违反 | → failed |
| 所有权失效 | 租约过期 / fencing 拒绝 | → attempt abandoned，先处理 Attempt，再由 Runtime 决定 recovering 或 retry_wait |

Deadline 执行：Worker 在每个节点边界检查 deadline_at；恢复扫描器把超期非终态任务转 failed(DEADLINE_EXCEEDED)。

## 9. 安全与审计

- **认证**（R1-Q7）：短时效 JWT HS256 ≤15 分钟，无 refresh token、无会话表；敏感工具执行前实时查 identity.region_grants——JWT 中旧权限不作准（权限撤销立即生效，HS-11）
- **Registry 管理**（R3-Q4）：无管理 API；迁移种子 + `scripts/registry.py`（创建/验证/发布）；发布不可变性 = DB 触发器/权限 + 服务层双重保证
- **审计八类动作**（R3-Q5）：任务创建、任务取消、工具调用授权决策（允许/拒绝+原因码）、认证失败与区域权限拒绝、Agent 版本发布与退役、评测运行发起、权限授予、权限撤销。追加写入；只读查询不审计，但安全拒绝必须记录
- 事件/日志/Trace 不含明文凭据；非 root 容器；无 Docker Socket 挂载；数据库不默认暴露

## 10. 种子数据集（R2-Q5）

确定性生成：固定随机种子 + 固定生成器版本 + 规范化哈希算法（SHA-256）+ 固定排序规则——四者固定保证任何人任何时间重建出同一 `data_snapshot_id`。

构成：3 区域 × 180 天 × 日均约 200 单 ≈ 10.8 万订单。内嵌 fixture：零基线窗口（BA-03）；约 3% 退款单（BA-07）；跨月跨时区边界（BA-05）；未支付/已取消单（BA-06）；华东环比精确 −18% 窗口（BA-02 断言 −0.1800）；其它区域数据（BA-08）。内容哈希随 seed 迁移固化，入库即不变。

## 11. 评测（R2-Q3）

- 全部评测 Task 归属种子服务账号 `svc-eval@agentforge.internal`（仅 fixture 区域只读授权）；用户任务列表按 owner 过滤，测试结果天然不混入
- dataset 显式绑定 data_snapshot_id；评测发起时校验 Task 快照 == dataset 快照，不一致该 case 直接判失败
- 默认 Fake Model（Offline）；Live（真实 DeepSeek）显式开关 + 调用预算上限
- 断言确定性：数值/变化率/证据引用结构校验；不把真实模型一次输出成功当确定性保证；Live 记录模型版本、参数、时间、成本、失败
- 内部 Runner 执行，无评测编辑器

## 12. 目录结构

```
agentforge/
├── apps/{api,worker,web}/          # 进程启动、装配、接口适配
├── packages/
│   ├── core/                       # contracts、errors、security；不依赖业务
│   ├── identity/  ├── registry/
│   ├── runtime/                    # domain、services、scheduler、recovery、persistence、events
│   ├── harness/                    # context、models、tools、policies
│   ├── agents/business_analysis/
│   ├── metrics/                    # semantic、compiler、executor；不依赖 LLM Provider
│   ├── evaluation/  └── observability/
├── scripts/registry.py             # Registry 管理 CLI（R3-Q4）
├── migrations/{versions,seeds}/
├── tests/{unit,integration,contract,recovery,security,evaluation,e2e}/
├── benchmarks/business_analysis_v1/
├── infra/{compose,docker,postgres}/
└── docs/{adr,architecture,api,operations,evidence}/
```

依赖原则：runtime 不依赖具体 Agent；harness 不依赖 FastAPI；evaluation 走公开运行接口不改任务状态。

## 13. 部署（Docker Compose，ADR-006）

**必需服务**：web · api（单副本）· worker（含恢复扫描协程）· postgres · migrate（一次性）· seed（一次性，开发环境）。
**可选 profile**（R1-Q2）：redis · tempo · prometheus · grafana。
P0 不需要：Kafka、Kubernetes、Elasticsearch、向量数据库、独立 recovery 服务。

启动顺序：PostgreSQL healthy → Migration completed → Seed（开发）→ API/Worker → Web。

可观测性基线（R1-Q2）：结构化 JSON 日志 + trace_id 全链路贯穿 + OTel SDK 就绪；Tempo/Prometheus/Grafana 为可选 profile，不在 Release Gate。

资源安全：非 root、只读根文件系统（支持的服务的）、无明文密钥、Worker 内存/CPU/并发限制、日志脱敏。

## 14. 测试与验收

### Runtime（RT-01~15）

创建 202 持久化；同键同请求重放；同键异请求 409；并发领取唯一有效租约；心跳续期；Worker 崩溃进恢复流程；**旧执行者提交被 fencing 拒绝**；**步骤确认后崩溃从已确认步骤恢复（重放-记忆化）**；**工具成功后崩溃复用已确认结果**；取消后不再启动新工具；重试耗尽 failed；Deadline 超时终止；SSE 断线重连不丢持久化事件；**RT-14（改写，R3-Q2）：NOTIFY 丢失 / 监听中断 / API 重启后，已提交事件仍可从 task_events 读取并经 SSE 补发**；完成与取消并发只有唯一合法终态。

### Harness 与安全（HS-01~12）

上下文超预算按策略裁剪；非法 JSON 有限重试；未绑定工具拒绝；非法参数拒绝；未授权区域 403；自由 SQL 拒绝；注入参数化；DB 账号 UPDATE 权限拒绝；超范围查询拒绝；Prompt Injection 越权被阻断；运行中撤权后工具调用拒绝；已发布配置不可变（含 DB 层触发器验证）。

### 经营分析（BA-01~10）

增长正确计算；−18% → −0.1800；零基线 → ZERO_BASELINE；本期无订单 → 零值+说明；跨月跨时区边界；未支付不计入；已退款仍计入（冻结口径）；区域混合只返授权区域；虚构原因标记假设或核验失败；报告引用不存在证据校验失败。

### 故障注入

按 §6.4 崩溃窗口精确触发（故障注入点，不靠随机杀进程）。**必须证明重放恢复不会在 step_executions、工具结果与图状态之间产生错误的成功状态**——恢复正确性本身在验收范围内，不可移除。

### 测试环境

| 环境 | 模型 | 数据 | 目的 |
|------|------|------|-----|
| Offline | Fake | 固定快照 | 快速确定性 |
| Integration | Fake | 真实 PostgreSQL | 事务、并发、恢复 |
| Live | DeepSeek | 固定业务快照 | 端到端验收（显式开启） |

### Release Gate

- Runtime：状态机全合法；并发领取唯一执行者；可恢复故障测试 100% 过；旧租约提交 100% 拒
- 安全：无自由 SQL 路径；未授权调用 100% 拒；跨区域泄漏 0 次；已发布配置不可变
- 业务：10 案例确定性校验全过；报告引用可追溯；真实模型 ≥3 端到端任务
- 可观测：Task 可关联 Trace ID；模型/工具结构化记录；失败定位到步骤与错误类别
- 交付：Compose 冷启动成功；迁移与种子可重复执行；API 文档与运行手册完整；前后端核心流程可演示
- **证据要求**：测试报告、迁移版本、API 记录、崩溃恢复记录、权限拒绝记录、真实模型调用记录、Trace 导出、健康检查结果。「设计完成」「代码已写」不替代运行验收

## 15. 里程碑（R3-Q6）

| 批次 | 交付 | 时间预算 | 门槛 |
|------|------|-------|------|
| M1 | 数据库、迁移、Identity、Registry | 1 周 | 迁移与权限测试过 |
| M2 | Task、Attempt、租约、幂等、事件 | 1 周 | Runtime 并发测试过 |
| M3 | Worker、step_executions 恢复协议 | 1 周 | 崩溃恢复测试过 |
| M4 | Harness、Model Adapter、Tool Gateway | 1 周 | 工具安全测试过 |
| M5 | 语义层、SQL 编译器、经营分析闭环 | 1–2 周 | 真实业务闭环过 |
| M6 | 评测、SSE、可观测、部署验收 | 1–2 周 | Release Gate 过 |

批次串行验收；不并行修改 Runtime 状态机与恢复协议。省下的进度余量优先给 M5。

## 16. P1 边界与强制前置条件

P0 明确不做、P1 进入前必须重开的设计：

1. **持久化 LangGraph Checkpointer**（ADR-0011）：并发写入隔离、分支恢复、fencing 适配层——验证完成前不得视为已具备能力。附录 A 的研究结论是本项选型输入：官方 Postgres Checkpointer 无并发写保护（last-writer-wins）、图调用会重置用户自设命名空间、跨线程 copy_thread 未实现——若 P1 采用，自研 fencing 写入适配层为必做项
2. 写工具与副作用对账协议（unknown 先对账再重试）
3. Outbox / 跨服务通知（webhook 等）
4. 多副本限流（Redis/网关）、事件保留期与归档
5. Registry 管理 API + RBAC；退款净收入指标；生产级数据快照（时点查询）
6. Supervisor + 六专业 Agent（ADR-003 目标架构）

## 17. ADR 索引

| ADR | 决策 | 状态 |
|-----|------|------|
| 0001 | 自研薄层 Harness + LangGraph | accepted |
| 0002 | PostgreSQL Worker + Checkpointer | amended by 0011 |
| 0003 | Supervisor + 6 专业 Agent（分阶段） | accepted |
| 0004 | 语义层 + 平台 SQL 编译器 | accepted |
| 0005 | 自研 Eval Runner + 固定 Benchmark | accepted |
| 0006 | Docker Compose 优先 | accepted |
| 0007 | Task 状态与 Checkpoint 分离 | superseded by 0011 |
| 0008 | 租约 + Fencing + Attempt 隔离 | accepted |
| 0009 | 业务数据快照与证据绑定 | accepted |
| 0010 | P0 只读经营分析 | accepted |
| 0011 | P0 进程内 MemorySaver，重放恢复 | accepted |

## 附录 A：LangGraph / DeepSeek 事实研究（2026-10-26，P1 选型输入）

> 来源：官方文档 + 源码核验（langgraph==1.2.17、langgraph-checkpoint-postgres==3.2.0、DeepSeek API docs）。逐条结论与出处见研究记录；此处为设计相关摘要。

**LangGraph 持久化 Checkpointer**

- **无并发写保护**：`put()` 为 last-writer-wins upsert——无版本比较、无 CAS、无 fencing；两个进程并发写同一线程双方都成功
- **命名空间隔离对图调用不可用**：用户自设的 `checkpoint_ns` 会被运行时重置为 `""`，命名空间仅由运行时为子图分配；按 Attempt 隔离必须绕过 `graph.invoke` 直接驱动 checkpointer API
- **线程内分支可行**：以历史 `checkpoint_id` 恢复、`update_state` 从历史检查点分叉（原件不改）；**跨线程 `copy_thread` 基类未实现、PostgresSaver 未提供**
- AsyncPostgresSaver 与同步版功能对等；表结构由 `checkpointer.setup()` 自建迁移管理
- **对 v1.0 十四-A 的裁决**：其首选方案（per-attempt 命名空间 + 从恢复点创建执行分支）开箱即用**不可行**——实证支持 ADR-0011 的 P0 决策；P1 采用时 fencing 写入适配层为强制前置

**DeepSeek API**

- Chat Completions 仅 `response_format: json_object`（无 json_schema）；Prompt 须含 "json" 与示例，否则可能无限空白直至 token 上限；可能出现空 content；`finish_reason=length` 截断输出
- 结构化强约束的可选路径：Responses API 的 json_schema，或 Beta strict tool-call（`/beta` endpoint，JSON-Schema 子集，无 minLength/maxLength 等约束）
- 工具调用 OpenAI 兼容；Chat API 不支持中途插入客户端伪造的 tool-call 消息
- 限流为**账号级并发数**（flash 2500 / v4-pro 500），超限 429；排队 10 分钟未开始推理即被服务端断开。P0 单并发场景无虞，现有超时参数已覆盖
