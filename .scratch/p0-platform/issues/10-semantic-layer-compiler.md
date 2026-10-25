# 10: 语义层与 SQL 编译器泛化

**What to build:** 把 07 号的模板化 GMV 查询泛化为 metric_definitions 驱动的完整编译管线：MetricPlanner → SemanticValidator → QueryCompiler → SQLValidator → ReadOnlyExecutor → ResultNormalizer。模型产出的 Metric Query Plan 是唯一输入，只接受注册指标/维度/过滤；全部安全不变量在编译层强制。07 号的 BA 断言在新管线下必须保持绿色（先让变化容易，再做容易的变化）。

**Blocked by:** 07

**Status:** ready-for-agent

- [ ] Metric Query Plan 只接受注册指标/维度/过滤；未注册 → 422 QUERY_PLAN_INVALID
- [ ] 管线六段成型，模板路径被完全替换；结果经 ResultNormalizer 归一
- [ ] 编译产物 AST 检查：拒绝多语句、DDL、DML、未支持函数（HS-06 纵深防御）
- [ ] 模型提交自由 SQL 片段/表名 → 拒绝（HS-06）
- [ ] 过滤值全部参数化绑定，注入 payload 不被执行（HS-07）
- [ ] 执行角色无业务写权限（HS-08）；固定 search_path
- [ ] 超出最大时间范围/返回行数/查询时长 → 拒绝（HS-09）
- [ ] BA-01~08 全部断言在新管线下无回归
