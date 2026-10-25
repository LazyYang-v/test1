---
status: accepted
---

# 租约 + Fencing + Attempt 隔离（ADR-008）

Task 的每次执行创建新 Attempt 并递增 fencing epoch；Worker 的一切写入——终态提交、事件、步骤确认、工具结果接纳——必须携带 lease_token 与 fencing_epoch，并在同一数据库事务内校验其仍为当前有效租约。租约失效即丧失对该 Task 的一切写入权；旧执行者的提交被拒绝（RT-07），其 Attempt 标记 abandoned。

## Consequences

- 取消是协作式的：已发出的外部调用无法强制中止，正确性完全依赖 fencing 阻止旧执行者污染状态。
- 外部只读查询在执行前与结果接纳前双重校验租约，并配合数据库侧 statement_timeout，不能只依赖应用层取消信号。
- 旧 Attempt 的工具结果不能直接当作新 Attempt 的有效结果，必须经当前租约及 fencing 校验接纳。
