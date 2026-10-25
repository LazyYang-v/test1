# AgentForge

企业级 Agent Harness 与 Runtime 平台：以受控、可恢复、可审计的方式执行经营分析 Agent 任务。P0 业务场景为电商企业经营分析。

## Language

### 运行时

**Task（任务）**:
用户提交的一项经营分析工作单元，拥有独立生命周期与唯一终态；是平台可靠性的作用对象。
_Avoid_: Job、请求

**Attempt（执行尝试）**:
Task 的一次受租约保护的执行；同一 Task 同一时刻至多一个有效 Attempt。
_Avoid_: Run、执行（单独使用时）

**Lease（租约）**:
Worker 对某个 Task 的限时执行所有权；靠心跳续期，失效即丧失对该 Task 的一切写入权。
_Avoid_: 锁（Lock）

**Fencing Epoch（栅栏纪元）**:
随每次新 Attempt 递增的单调编号；携带过期纪元的旧执行者的一切提交都会被拒绝。
_Avoid_: 版本号

**Evidence（证据）**:
平台生成并持久化的一次受控指标查询的不可变结果记录；模型只能引用，不能生成或修改。
_Avoid_: 数据、结果（与 Evidence 混用时）

**Data Snapshot（数据快照）**:
与 Task 绑定的业务数据集身份（内容哈希）；同一 Task 的全部 Evidence 必须出自同一快照。
_Avoid_: 数据版本、备份

**Checkpoint（检查点）**:
图执行进度的状态快照；P0 仅存在于 Worker 进程内，跨崩溃恢复以步骤执行记录为准，不以检查点为准。
_Avoid_: 恢复点（那是已确认的 Step Execution）

**Step Execution（步骤执行记录）**:
以稳定步骤键标识的一个图节点执行的持久化记录；其已确认子集是跨崩溃恢复的真相源与重放起点。
_Avoid_: 检查点、日志

**Metric Query Plan（指标查询计划）**:
模型表达指标查询意图的唯一形式——结构化、非 SQL 的计划，由平台语义层校验并编译为参数化只读 SQL 执行。
_Avoid_: SQL、查询语句

### 业务

**GMV**:
指定期间内曾成功支付订单的实付金额合计，按支付成功时间归集，不扣除后续退款。
_Avoid_: 销售额、流水

**订单状态**:
`pending`（待支付）、`paid`（已支付）、`refunded`（已退款，仍计入 GMV）、`cancelled`（已取消）；「曾支付」= paid 或 refunded。
_Avoid_: 订单类型
