---
status: amended by 0011
---

# PostgreSQL Worker + Checkpointer（ADR-002）

任务状态、租约、事件、幂等记录全部落在单个 PostgreSQL 实例，由 Worker 进程池以租约方式认领执行，不引入独立消息队列或调度服务——运维面最小，事务边界清晰。原决策同时采用 LangGraph 官方 PostgreSQL Checkpointer 持久化图进度；该部分经 ADR-0011 修订：P0 改用进程内 MemorySaver，持久化 Checkpointer 推迟至 P1 并附强制验证前置条件。

## Consequences

- PostgreSQL 是 Task 生命周期与恢复的唯一持久化真相源。
- Worker 水平扩展靠 SKIP LOCKED 认领，无需协调服务。
