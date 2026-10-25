# 07: 真实指标执行——只读 SQL、Evidence、变化率

**What to build:** execute_metrics 从桩换成真实路径：经 Tool Gateway 以模板化参数化只读 SQL 查询授权视图（business_ro 角色），区域过滤由平台强制注入；每次查询产出不可变 Evidence（查询计划、结果集哈希、行数、data_snapshot_id）；变化率由确定性计算器产生。首个业务用例（华东 7 天 GMV 环比）端到端真实闭环。

**Blocked by:** 06

**Status:** ready-for-agent

- [ ] GMV 查询经只读角色执行：参数化绑定、区域强制注入、授权区域混合数据下只返回授权区域（BA-08）
- [ ] 每次查询产出 Evidence 并持久化：查询计划（脱敏）、结果集哈希、行数、快照 ID；同一 Task 内全部证据快照一致
- [ ] 变化率由确定性计算器产生：下降 18% → "-0.1800"；上期为零 → null + ZERO_BASELINE，无除零（BA-01/02/03）
- [ ] 本期无订单 → 零值 + 数据不足说明（BA-04）；跨月跨时区边界正确（BA-05）
- [ ] 报告数值与 Evidence 相符：validate_report 校验引用存在、归属当前 Task、数值一致（BA-10）
- [ ] 工具调用唯一约束 (task_id, invocation_key) 生效；同请求重复执行复用持久化结果
- [ ] 模型节点（Fake）不能影响数值：计划只决定查询什么，数值全部来自平台计算
