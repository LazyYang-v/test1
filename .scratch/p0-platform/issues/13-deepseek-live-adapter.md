# 13: DeepSeek 适配器与 Live 冒烟

**What to build:** 真实 Provider 适配：DeepSeek 与 Fake Model 经同一 ModelAdapter 协议配置互换；请求遵守已核实约束（json_object 模式、Prompt 含 "json" 字样与示例、空 content 与 finish_reason=length 截断处理、有限修复重试）；至少一次真实模型端到端任务并完整留痕。

**Blocked by:** 07

**Status:** claimed

- [x] DeepSeek 与 Fake Model 经不可变模型快照选择同一 ModelAdapter 协议，不改图路径
- [x] 请求侧约束落实：Prompt 含 "json" 与 JSON 示例；json_object 模式；空 content 与截断被视为可修复失败而非成功
- [x] 输出不符 Schema 有限修复重试，超预算失败；绝不静默生成默认结论
- [x] 每次调用记录 Provider 请求 ID、模型版本、参数、Token 用量、成本、时长（migration 0012）
- [x] Provider 5xx/超时/限流归类为可重试，与 08 号分类表一致
- [ ] ≥1 次真实模型端到端任务 completed，全程记录可追溯（deferred to Release Gate; no provider call made for this ticket）

## Comments

2026-10-26: Implementation on `ticket/13-deepseek-live-adapter`, starting from ticket 08 handoff `2185d8a` (which contains integration/p0 through `ac8ee23`). Fake/DeepSeek adapter selection, mocked provider contract, bounded JSON validation/repair, transient error classification, and invocation metadata persistence are implemented. Offline/unit verification only; shared Docker/PostgreSQL integration tests are deferred until coordinated. No live DeepSeek call was made and provider spend is zero. The real live run is deferred to Release Gate.
