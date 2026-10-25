# 09: 崩溃恢复与故障注入

**What to build:** 恢复能力的完整证明：八个崩溃窗口（基线 §6.4）各设精确触发的故障注入钩子；Worker 内恢复扫描器（SKIP LOCKED）处理租约过期——旧 Attempt 标记 abandoned，新 Attempt 以重放-记忆化从已确认 Step Execution 续跑，工具结果按 invocation_key 复用；旧执行者的一切提交被 Fencing 拒绝。必须证明重放恢复不会在步骤记录、工具结果与图状态之间产生错误的成功状态。

**Blocked by:** 06、08

**Status:** ready-for-agent

- [ ] 八个崩溃窗口（创建事务前后、领取后、模型期间/返回后、工具落库前后、步骤确认后、终态前）各有精确注入点，测试不依赖随机杀进程
- [ ] 心跳停止 → 租约过期 → 扫描器标旧 Attempt abandoned → 任务经 recovering 恢复流转（RT-06）
- [ ] 步骤确认后崩溃：新 Attempt 从已确认步骤续跑，不重调模型/工具（RT-08）
- [ ] 工具结果落库后崩溃：按 (task_id, invocation_key) 复用结果，不重复查询（RT-09）
- [ ] 旧执行者（过期租约/fencing epoch）提交终态、事件、步骤确认 100% 被拒（RT-07）
- [ ] 无法确认恢复状态 → failed + 诊断证据保留，绝不伪造成功
- [ ] 恢复期间模型调用如发生重复，成本如实记账（model_invocations 如实记录）
- [ ] 故障注入测试全部确定性可重复运行
