# 08: 取消、重试、Deadline、错误分类

**What to build:** 任务生命周期逆境路径全部落地：取消（queued 直接 cancelled、running 协作式经 cancelling）、冻结的错误分类表驱动 retry_wait/failed、退避调度（next_retry_at）、Attempt 耗尽与 Deadline 终止。取消是协作式的——已发出的调用无法强制中止，正确性靠 fencing 保证。

**Blocked by:** 06

**Status:** ready-for-agent

- [ ] queued 任务取消直接 cancelled；running 进入 cancelling，执行者在节点边界与工具调用前检测后安全停止（RT-10）
- [ ] 取消后不再发起新的模型/工具调用；cancelling 下 Worker 失联走 recovering
- [ ] 可重试错误（Fake 注入 provider 超时/5xx）→ retry_wait + next_retry_at（30s+抖动）；到期自动回 queued 被重新认领
- [ ] 不可重试错误（Schema 修复耗尽、权限拒绝、BUDGET_EXCEEDED、内部不变量违反）直接 failed 带错误码
- [ ] 最大 Attempt 耗尽 → failed（RT-11）；Deadline 到期 → failed(DEADLINE_EXCEEDED)，由节点边界检查与扫描器双路执行（RT-12）
- [ ] 取消与完成并发竞争只产生一个合法终态（RT-15）
- [ ] 取消请求（含乐观版本）冲突时返回 409 INVALID_STATE_TRANSITION
