---
status: superseded by 0011
---

# Task 状态与 Checkpoint 分离（ADR-007）

原决策：PostgreSQL Runtime 状态与 LangGraph Checkpoint 是两个真相源（任务生命周期 vs 图执行进度），须通过恢复协议对账协调，不能假设二者原子一致。

**已被 ADR-0011 吸收**：P0 不使用持久化 Checkpointer 后，只保留平台数据库一个持久化真相源（`step_executions` 即恢复点），双真相源对账问题在 P0 不存在。P1 引入持久化 Checkpointer 时，本决策的对账问题将重新出现。
