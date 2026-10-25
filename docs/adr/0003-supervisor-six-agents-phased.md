---
status: accepted
---

# Supervisor + 六专业 Agent，分阶段落地（ADR-003）

目标架构是 Supervisor 编排六个专业 Agent（经营分析为首个场景），但多 Agent 分支图对图状态持久化与恢复的要求远超 P0 所需。决定：分阶段——P0 先用线性四节点图验证 Harness 与 Runtime 底座，Supervisor + 六 Agent 在 P1 及以后基于已验证的执行基础引入。

## Consequences

- P0 的恢复协议只需处理线性重放；分支图恢复（含持久化 Checkpointer）成为 P1 强制前置（见 ADR-0011）。
