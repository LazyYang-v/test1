---
status: accepted
---

# Docker Compose 优先（ADR-006）

P0 的部署复杂度预算为零：单机 Docker Compose 即满足全部验收场景（含故障注入与冷启动验收），Kubernetes、Kafka、Elasticsearch、独立向量数据库均不引入。Tempo/Prometheus/Grafana 以可选 profile 存在，不进入 Release Gate（v1.1 裁剪，R1-Q2）。

## Consequences

- 单 API 副本下的限流为进程内实现，不宣称多副本全局限流。
- P1 多副本/多租户时重开部署与限流设计。
