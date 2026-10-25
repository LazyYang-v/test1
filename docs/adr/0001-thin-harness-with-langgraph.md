---
status: accepted
---

# 自研薄层 Harness + LangGraph（ADR-001）

企业级 Agent 平台需要对上下文组装、模型适配、工具网关、策略引擎的完全控制权，现有框架无法提供这些可审计的接缝。决定：自研薄层 Harness（ContextBuilder / ModelAdapter / ToolRegistry / ToolGateway / PolicyEngine / ExecutionBudget，均为 Python Protocol），LangGraph 仅承担图编排职责。

## Consequences

- 平台不继承任何框架的持久化与安全语义；状态、恢复、审计全部由平台数据库承载。
- LangGraph 可整体替换（P0 中它只是进程内编排器，见 ADR-0011）。
