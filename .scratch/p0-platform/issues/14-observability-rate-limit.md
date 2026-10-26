# 14: 可观测性与限流

**What to build:** trace_id 全链路贯穿（API 请求 → Worker 执行 → 模型/工具调用 → 任务事件 → 审计）；结构化 JSON 日志（脱敏）；OTel SDK 就绪；API 按用户进程内令牌桶限流。P0 不部署 Tempo/Prometheus/Grafana（可选 profile），但 SDK 与导出配置就绪。

**Blocked by:** 06

**Status:** claimed

2026-10-26: Implementation started on `ticket/14-observability-rate-limit-work` from `integration/p0` tip `ac8ee23`. Scope: additive Task trace ID migration 0009, structured redacted API/Worker logs, optional OTel readiness, and bounded per-principal API token-bucket limiting. Main seam: HTTP API with real PostgreSQL and deterministic fake-clock limiter tests. Full Docker suite deferred while ticket 08 verification is active.

- [ ] 每个 Task 有 trace_id，出现在 API 日志、Worker 日志、model_invocations、tool_invocations、task_events、audit_events 中
- [ ] 结构化 JSON 日志：无明文凭据、无完整敏感 Prompt、无多余业务原始数据
- [ ] OTel SDK 就绪，导出端点可配置、默认关闭
- [ ] 按用户令牌桶限流：超限 429 + Retry-After；限流语义为单进程，不宣称多副本全局
- [ ] 任一失败任务可凭 trace_id 定位到步骤与错误类别（Release Gate 观测项）
- [ ] Compose 中 Tempo/Prometheus/Grafana/Redis 仅存在于可选 profile，缺省冷启动不依赖它们
