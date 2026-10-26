# 16: Release Gate 组装

**What to build:** P0 收口：全量冷启动复验（必需服务 + 可选 profile 分离）、API 文档与运行手册、Release Gate 各项运行证据归集（测试报告、崩溃恢复记录、权限拒绝记录、≥3 次真实模型端到端、Trace 导出、健康检查），以及对全部不可变检查的最终复核。以运行证据验收，不以「代码已写」验收。

**Blocked by:** 09、10、11、12、13、14、15

**Status:** claimed

- [ ] `docker compose up` 一键冷启动全部必需服务，健康检查全绿；可重复执行（seed 服务与可选 observability profile 已交付；最终提交上的冷启动复验 BLOCKED-ENVIRONMENT）
- [ ] 迁移与种子在任何环境可重复执行，data_snapshot_id 保持不变（loader 幂等性已验证；最终提交上复验 BLOCKED-ENVIRONMENT）
- [x] API 文档（端点、错误码表、SSE 去重与 at-least-once 语义）与运行手册完整（`docs/api.md`、`docs/runbook.md`，全部从源码核实）
- [x] Release Gate 证据归集：`docs/verification/release-gate-evidence.md` 逐项索引；需 Docker/真实模型执行的项目显式标注 BLOCKED-ENVIRONMENT 并附执行命令
- [ ] 不变量复核：跨区域数据泄漏 0 次、未授权工具调用 100% 拒绝、旧租约提交 100% 被拒、已发布配置不可变、并发领取唯一执行者（由集成测试与数据库触发器强制；最终提交上的汇总执行 BLOCKED-ENVIRONMENT）
- [ ] 10 个核心经营分析案例全部通过确定性指标校验；报告引用可追溯（`test_evaluations.py` 就绪；集成执行 BLOCKED-ENVIRONMENT）

## Comments

- 2026-10-27 (orchestrator): deliverables merged into `integration/p0` at `afbda79` (compose seed service + optional observability profile, `agentforge-dataset`/`agentforge-seed` console scripts) and `02b85c4` (docs). The runtime-evidence items require a Docker-capable host with optional DeepSeek access; exact commands are in `docs/verification/release-gate-evidence.md`. Subagent orchestration was unavailable in this session, so implementation and review were performed directly in the coordinator session.
