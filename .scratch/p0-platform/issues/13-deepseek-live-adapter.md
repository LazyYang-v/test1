# 13: DeepSeek 适配器与 Live 冒烟

**What to build:** 真实 Provider 适配：DeepSeek 与 Fake Model 经同一 ModelAdapter 协议配置互换；请求遵守已核实约束（json_object 模式、Prompt 含 "json" 字样与示例、空 content 与 finish_reason=length 截断处理、有限修复重试）；至少一次真实模型端到端任务并完整留痕。

**Blocked by:** 07

**Status:** ready-for-agent

- [ ] DeepSeek 与 Fake Model 经配置切换互换，不改代码路径
- [ ] 请求侧约束落实：Prompt 含 "json" 与示例；json_object 模式；空 content 与截断被视为可修复失败而非成功
- [ ] 输出不符 Schema 有限修复重试，超预算失败；绝不静默生成默认结论
- [ ] 每次调用记录 Provider 请求 ID、模型版本、参数、Token 用量、成本、时长
- [ ] Provider 5xx/超时/限流归类为可重试，与 08 号分类表一致
- [ ] ≥1 次真实模型端到端任务 completed，全程记录可追溯
