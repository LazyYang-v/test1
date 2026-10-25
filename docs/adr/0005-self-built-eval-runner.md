---
status: accepted
---

# 自研 Eval Runner + 固定 Benchmark（ADR-005）

P0 的评测目标是确定性回归：固定案例、固定数据快照、对数值与证据引用做结构断言，不引入 LLM Judge（其非确定性会污染回归信号）。决定：自研轻量 Runner 驱动真实 Runtime 执行，评测结果与业务任务经服务账号隔离（见基线 §11）。

## Consequences

- 「模型虚构原因」类案例只能断言结构（hypotheses 不冒充 findings），不能断言措辞。
- LLM Judge 与评测控制台留给后续阶段。
