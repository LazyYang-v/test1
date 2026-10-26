# 12: 评测 Runner

**What to build:** 固定 Benchmark 经公开 API 驱动真实任务并产出确定性结论：数据集与案例落库（dataset 绑定 data_snapshot_id）、评测任务统一归属 svc-eval 服务账号（仅 fixture 区域只读）、发起时校验快照一致、案例结果含确定性断言（数值、变化率、证据引用结构）。默认 Fake Model，Live 模式显式开启且受调用预算约束。

**Blocked by:** 07

**Status:** claimed

- [ ] datasets/cases/runs/case_results 落库；dataset 显式绑定 data_snapshot_id
- [ ] `POST /evaluations/runs` 发起评测；评测任务 owner 为 svc-eval，不出现在普通用户任务列表
- [ ] 发起时校验任务快照 == dataset 快照，不一致该 case 直接判失败
- [ ] 10 个核心案例（BA-01~10）确定性断言全部通过；结果含 status/score/failure_codes/duration
- [ ] 「模型虚构原因」类案例以结构断言覆盖：hypotheses 不得冒充 findings、findings 必须引用有效证据
- [ ] 默认 Fake Model；Live 模式需显式开关 + 调用预算，超预算立即停止
- [ ] 评测不改写任务原始结果，只读取与判定
