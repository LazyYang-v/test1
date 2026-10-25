# 05: Task 创建、幂等、查询

**What to build:** `POST /api/v1/tasks` 完整落地：校验身份、区域子集、日期范围（≤90 天、左闭右开）、已发布 Agent 解析，创建 Task（绑定 data_snapshot_id 与 authorization_snapshot）返回 202 并落首个事件。幂等键全语义：同键同请求重放同一响应（含并发处理窗口），同键异请求 409。任务详情与分页列表查询。

**Blocked by:** 02、03、04

**Status:** ready-for-agent

- [ ] 合法请求 202 + task_id + links；Task 状态 queued，task.created 事件落库
- [ ] agent_key 解析不到已发布版本 → 拒绝；region_codes 非当前授权子集 → 403 REGION_ACCESS_DENIED；日期范围超 90 天 → 拒绝
- [ ] 同幂等键同规范化请求重放同一 202 响应，含首个请求仍在处理中的并发窗口（RT-02）
- [ ] 同幂等键不同请求 → 409 IDEMPOTENCY_CONFLICT（RT-03）
- [ ] 创建事务原子：任何失败不留半创建 Task
- [ ] `GET /tasks/{id}` 返回状态/版本/current_attempt_no/进度占位；他人任务返回 404 不泄露存在性
- [ ] 任务列表按用户、状态、日期分页
- [ ] 统一错误体（code/message/retryable/request_id）覆盖全部错误路径
