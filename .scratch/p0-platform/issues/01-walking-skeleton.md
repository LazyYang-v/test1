# 01: 行走骨架——Compose 冷启动、迁移骨架、健康检查

**What to build:** 一条命令 `docker compose up` 冷启动平台：PostgreSQL 就绪、迁移框架执行首个迁移（全部 Schema、数据库角色、扩展）、API 进程以非超级用户账号连库并通过 `/health/live` 与 `/health/ready`（Ready 校验迁移版本与核心依赖，非仅端口连通）。同时交付可对真实 PostgreSQL 运行的测试基建（Offline / Integration 两级）。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `docker compose up` 冷启动后 `/health/live` 返回 200
- [ ] 迁移未执行或版本不匹配时 `/health/ready` 拒绝就绪；迁移完成后返回 200
- [ ] 迁移 0001 创建全部 Schema 与五个数据库角色（migrator/api/worker/business_ro/eval），可重复执行不报错
- [ ] Integration 测试对真实 PostgreSQL 运行并在结束后清理状态
- [ ] API 服务使用非超级用户数据库角色；不自动执行破坏性迁移
