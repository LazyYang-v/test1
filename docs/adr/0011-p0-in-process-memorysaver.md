---
status: accepted
---

# P0 不使用持久化 LangGraph Checkpointer（修订 ADR-002）

LangGraph 官方 PostgreSQL Checkpointer 的并发写入隔离与分支恢复语义未经验证，而 P0 图为线性四节点、无分支无循环，平台步骤记录已足以承载恢复。决定：P0 的 LangGraph 使用进程内 MemorySaver；跨崩溃恢复以 `runtime.step_executions` 的重放-记忆化为准——已确认步骤复用持久化结果，未确认步骤重新执行，结果不确定先查持久化调用记录（只读工具可安全重查），无法确认恢复状态则明确失败并保留诊断，禁止伪造成功。ADR-002 的「持久化 Checkpointer」部分据此推迟。本决定吸收原 ADR-007（Task 状态与 Checkpoint 分离）：P0 只保留平台数据库一个持久化真相源。

## Consequences

- MemorySaver 不跨进程，不能作为恢复依据；Worker 重启后必须重建图执行上下文。
- `step_executions` 的事务性结果确认、稳定步骤键、fencing 校验成为恢复协议的承重点（见 ADR-0008）。
- 节点中途崩溃则整节点重跑；模型调用可能重复，成本如实记账。
- P1 引入分支图（Supervisor + 6 Agent）之前的强制前置条件：重开并验证持久化 Checkpointer、并发写入隔离、分支恢复与 fencing 适配层设计；验证完成前不得将其视为已具备的能力。
