# 11: SSE 事件流

**What to build:** `GET /tasks/{id}/events` 实时推送任务事件：事件持久化早于通知，SSE 以 task_event_seq 为事件 ID；LISTEN/NOTIFY 唤醒 + 10 秒兜底轮询；断线经 Last-Event-ID 补发。PostgreSQL 是唯一事件真相源，NOTIFY 只当唤醒信号。

**Blocked by:** 06

**Status:** ready-for-agent

- [ ] 事件先持久化后通知；SSE id = task_event_seq，任务内严格递增
- [ ] 支持 Last-Event-ID：断线重连从该序号补发全部后续事件（RT-13）
- [ ] NOTIFY 丢失、监听连接中断、API 进程重启后，兜底轮询保证已提交事件不丢（RT-14 改写版）
- [ ] 通知协议时序正确：pg_notify 在事件事务内发出、提交后交付
- [ ] 连接断开不影响任务执行；API 文档注明客户端按序号去重与 at-least-once 语义
- [ ] 未授权用户订阅他人任务事件被拒
