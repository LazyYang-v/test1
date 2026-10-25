# 06: 第一发曳光弹——任务完整跑通（指标桩）

**What to build:** Worker 认领 queued 任务并完整执行四节点图（plan_metrics → execute_metrics → compose_analysis → validate_report）：Fake Model 产出查询计划与分析说明、指标节点先用确定性桩、Tool Gateway 九步安全骨架就位、每步确认写 Step Execution 与事件、任务进入 completed 并持久化符合报告 Schema 的结果。这是贯穿 Runtime/Harness/图/网关的第一条完整路径——数值尚来自桩，07 号换真实查询。

**Blocked by:** 05

**Status:** ready-for-agent

- [ ] Worker 以 SKIP LOCKED 认领；两 Worker 并发领取只有一个有效租约（RT-04）
- [ ] 心跳按间隔续期租约（RT-05）；Attempt 记录 worker/lease_token/fencing_epoch
- [ ] 四节点顺序执行；每步确认在事务内验证租约后写 step_executions（稳定 step_key，如 plan_metrics:0）+ task_event
- [ ] Fake Model 输出可脚本化；非法 JSON 经校验有限修复重试、超预算失败而非默认结论（HS-02）
- [ ] Tool Gateway 骨架：工具版本验证、输入 Schema 校验、未绑定工具拒绝（HS-03）、非法参数拒绝（HS-04）、审计与 Trace 记录
- [ ] 结果符合报告 Schema：findings 引用有效证据 ID、hypotheses 标注待验证、limitations 齐全；validate_report 拒绝引用不存在证据（BA-10 结构）
- [ ] 任务 → completed，result_json 与 completed_at 持久化，事件序完整可读
- [ ] 数据库事务短小：节点执行期间不持有 Task 行锁
