# P0.5-A 身份、Bootstrap 与管理授权设计

- **状态**：待人工批准；仅设计，不授权编码、迁移或部署
- **日期**：2026-10-27
- **范围**：Migration 0013、一次性 Bootstrap、Operator 身份权限、用户与 Region Grant 管理、Registry 管理 API 的认证边界
- **本轮约束**：不改产品代码、不运行迁移、不创建账号、不配置 DeepSeek

## 1. 目标与边界

P0.5-A 给首个 Operator 建立可审计、一次性且本机受限的引导流程；使用现有用户登录、Argon2id 与短时效 JWT；把身份授权和 Registry 写操作改为服务端可验证的 Operator 权限；限制新授予的 Region Grant；保留 P0 的实时数据授权检查。

本设计不增加邮件邀请、refresh token、完整 `Idempotency-Key` 缓存、Operator 禁用/删除/权限管理 API，也不让 Operator 权限绕过 Task 所有权或 Region Grant。

## 2. 已冻结决策

1. Bootstrap secret 本机安全生成、带外提供；入口只接受可信本机访问，并且一次性、限时、原子消费、防重放。
2. 首个 Operator 同时有 `identity_admin` 和 `registry_operator`；两项权限由代码分别、精确检查。
3. 初始密码由管理员安全交付；不开发邮件邀请。
4. P0.5 只允许 `CN_EAST/read` Region Grant，其他授权默认拒绝。
5. Registry CLI 写操作改走认证管理 API；`actor_id` 从服务端身份上下文取得，不接受客户端 UUID。
6. `identity.users.email` 唯一；重复创建返回 409，管理员可查询确认；不做完整幂等响应缓存。
7. P0.5 管理接口不能禁用 Operator；Operator 生命周期管理单独设计。

## 3. 仓库现状核对

- `migrations/versions/0002_identity.sql` 已有 `identity.users`、`identity.region_grants` 和 `audit.audit_events`。邮箱有唯一约束并要求小写、去首尾空格；密码哈希可空；用户状态为 `active/disabled`；Region Grant 当前允许任意非空地区与 `read/admin`。
- `apps/api/src/agentforge_api/auth.py` 使用 Argon2id（`time_cost=2`、`memory_cost=19456`、`parallelism=1`），未知用户走 dummy hash；JWT 为 HS256，最长 900 秒；每个请求回查用户状态与 `token_version`。JWT 不含角色或 Region Grant。
- `apps/api/src/agentforge_api/app.py` 的 `/auth/login` 已有通用失败响应和认证失败审计；`/auth/me` 只返回身份与活跃 Region Grant。`current_principal` 的限流发生在认证后，现有登录路径没有登录尝试限流。
- Operator 表和 Operator 权限检查目前不存在。API 角色对身份表只有 `SELECT`，Registry 四表有 `SELECT`；`agentforge_registry_cli` 对 Registry 四表有 `SELECT/INSERT/UPDATE/DELETE`。
- `migrations/versions/0004_registry.sql` 的发布/退役触发器从事务级 `agentforge.actor_id` 读取 actor；没有值时记录 `actor_type='database'`。Registry CLI 通过 `--actor-id` 设置该值并记录为 `cli`。
- `audit.audit_events` 由 `agentforge_api`、Worker 追加写入；现有 API 访问日志记录路由、方法、request id 与状态码，不记录请求体、Authorization header 或响应体。
- P0 任务创建、任务详情/SSE、Evaluation 和 Worker Tool Gateway 都实时读取 `identity.region_grants`；多个 SQL 允许 `admin` 当作读取权限。
- `migrations/versions/0011_evaluation_runner.sql` 的 `svc-eval` 只获 `CN_EAST/read`。P0 测试则有 `east/west` 和 `CN_NORTH` 授权样例。
- 当前 Compose API 与 Web 端口短语法都没有 host IP，默认发布到宿主机所有接口。API 容器监听 `0.0.0.0:8000`；Web Nginx 将所有 `/api/` 转给 API。PostgreSQL 未发布宿主机端口。启用 observability profile 时，匿名 Viewer 的 Grafana 也会发布宿主机端口。

## 4. Migration 0013 详细设计

建议文件名：`migrations/versions/0013_identity_operator_bootstrap_management_api.sql`。迁移只新增状态表、限制未来的授权写入、调整服务角色权限和更新 Registry actor 审计；不创建 Operator、不存储任何明文凭据。

### 4.1 Operator 权限表

```sql
CREATE TABLE identity.operator_permissions (
    user_id UUID NOT NULL REFERENCES identity.users(id) ON DELETE RESTRICT,
    permission TEXT NOT NULL
        CHECK (permission IN ('identity_admin', 'registry_operator')),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by UUID REFERENCES identity.users(id) ON DELETE RESTRICT,
    PRIMARY KEY (user_id, permission)
);

REVOKE ALL PRIVILEGES ON identity.operator_permissions FROM PUBLIC;
GRANT SELECT, INSERT ON identity.operator_permissions TO agentforge_api;
```

权限是一条一条的独立记录，无宽泛 `operator/admin` 角色、无隐式继承、无状态列。首个 Operator 只由 Bootstrap 事务写入两条权限；P0.5 没有修改或撤销 Operator 权限的管理端点。`granted_by` 对 Bootstrap 初始两条记录为 `NULL`，审计事件以 `actor_type='bootstrap'` 表示引导操作。

### 4.2 一次性 Bootstrap 状态

```sql
CREATE TABLE identity.bootstrap_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    secret_digest BYTEA NOT NULL CHECK (octet_length(secret_digest) = 32),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    consumed_by UUID REFERENCES identity.users(id) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK ((consumed_at IS NULL) = (consumed_by IS NULL))
);

REVOKE ALL PRIVILEGES ON identity.bootstrap_state FROM PUBLIC;
GRANT SELECT, UPDATE (consumed_at, consumed_by)
    ON identity.bootstrap_state TO agentforge_api;
```

表最多一行。数据库仅保存 32-byte SHA-256 digest、签发/过期/消费状态和 Operator UUID，不保存 secret。Bootstrap 工具以 `agentforge_migrator` 连接写入 digest；API 只能读取 digest 和单向设置消费字段，不能改 digest、过期时间或清除消费状态。

迁移新增以下 `BEFORE UPDATE` trigger，阻止已消费状态被重置或改写。未消费 secret 的轮换只由本机 Bootstrap 工具执行；工具不得轮换已消费记录。

```sql
CREATE OR REPLACE FUNCTION identity.prevent_bootstrap_reuse()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, identity
AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL
       AND (NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
            OR NEW.consumed_by IS DISTINCT FROM OLD.consumed_by) THEN
        RAISE EXCEPTION 'consumed bootstrap state is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER bootstrap_state_consumption_guard
BEFORE UPDATE ON identity.bootstrap_state
FOR EACH ROW EXECUTE FUNCTION identity.prevent_bootstrap_reuse();

REVOKE EXECUTE ON FUNCTION identity.prevent_bootstrap_reuse()
    FROM PUBLIC, agentforge_api, agentforge_worker, agentforge_registry_cli,
         agentforge_business_ro, agentforge_eval;
```

### 4.3 Region Grant 收窄

先在事务内做 fail-closed 预检；若存在活跃的非 `CN_EAST/read` Grant，迁移报错并整体回滚，要求人工核对/处理，不自动撤权：

```sql
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM identity.region_grants
        WHERE revoked_at IS NULL
          AND NOT (region_code = 'CN_EAST' AND permission = 'read')
    ) THEN
        RAISE EXCEPTION
            'P0.5 requires all active Region Grants to be CN_EAST/read; reconcile first';
    END IF;
END
$$;

ALTER TABLE identity.region_grants
    ADD CONSTRAINT region_grants_p05_active_scope
    CHECK (
        revoked_at IS NOT NULL
        OR (region_code = 'CN_EAST' AND permission = 'read')
    ) NOT VALID;

ALTER TABLE identity.region_grants
    VALIDATE CONSTRAINT region_grants_p05_active_scope;
```

已撤销的历史行保留；未来的活跃行只能是 `CN_EAST/read`。API、Task、SSE、Evaluation 与 Worker 的实时授权 SQL 也显式只接受该组合，不能仅依靠新管理端点或数据库约束。

迁移在 `registry.guard_agent_version_mutation()` 的 lifecycle audit block 中替换 actor 解析与 INSERT 值；其余 immutability/policy 逻辑保留：

```sql
IF audit_action IS NOT NULL THEN
    actor_setting := NULLIF(current_setting('agentforge.actor_id', TRUE), '');
    IF actor_setting IS NULL THEN
        RAISE EXCEPTION 'authenticated operator context is required'
            USING ERRCODE = '42501';
    END IF;
    actor_uuid := actor_setting::uuid;
    INSERT INTO audit.audit_events
        (actor_type, actor_id, action, resource_type, resource_id, decision, metadata_json)
    VALUES (
        'operator', actor_uuid, audit_action, 'agent_version', NEW.id::text,
        'allowed', jsonb_build_object('agent_key', NEW.agent_key, 'version', NEW.version)
    );
END IF;
```

不改变既有邮箱唯一约束、Argon2 哈希格式、用户 `status`、JWT Claims 或 Registry 发布不可变触发器的配置保护。

### 4.4 服务端最小权限

```sql
GRANT INSERT ON identity.users TO agentforge_api;
GRANT SELECT, INSERT, UPDATE (revoked_at)
    ON identity.region_grants TO agentforge_api;

REVOKE INSERT, UPDATE, DELETE ON
    registry.model_profiles,
    registry.agent_versions,
    registry.tool_versions,
    registry.agent_tool_bindings
FROM agentforge_registry_cli;

GRANT INSERT ON registry.model_profiles, registry.tool_versions,
    registry.agent_versions, registry.agent_tool_bindings TO agentforge_api;
GRANT UPDATE (model_config_snapshot, config_hash, status, published_at)
    ON registry.agent_versions TO agentforge_api;
```

API 仍无 `identity.users` 的 `UPDATE` 或 `DELETE` 权限，管理 API 也不提供 status 变更，因此 Operator 不能被管理接口禁用。Registry CLI 保留四表 `SELECT`，写入权限全部撤回；所有 Registry 写路径必须通过 API。

Registry 发布/退役 trigger 保留现有不可变性规则，但 actor 读取改为必填事务级 `agentforge.actor_id`；缺失或非法时拒绝变更。审计 actor 类型改为 `operator`。管理 API 在同一事务内由已验证 principal 设置 `set_config('agentforge.actor_id', principal.id, true)`，请求模型不含 `actor_id`。API 对模型配置、工具、绑定、Agent 创建显式追加审计；发布/退役继续由 DB trigger 原子审计。

### 4.5 Schema 版本与迁移纪律

把 `packages/core/src/agentforge_core/schema_version.py` 从 12 提升至 13；readiness 在 0013 未应用时继续阻止 API Ready。新增迁移保持单事务、连续版本和 checksum；不改已发布的 0001–0012。

## 5. API Schema 与状态机

所有新请求模型 `extra='forbid'`。管理接口不得接收 `actor_id`；错误响应不回显密码、secret、JWT 或 SQL/driver 错误。

### 5.1 Bootstrap

`POST /api/v1/bootstrap`：未登录入口，只能通过宿主机 loopback 入口访问；secret 与初始 Operator 密码只在 HTTPS（部署环境）或本机 loopback 的 JSON body 中传输，不放 URL、header、日志或 audit metadata。

```json
{
  "bootstrap_secret": "<one-time secret>",
  "email": "operator@example.com",
  "display_name": "Initial Operator",
  "initial_password": "<administrator-delivered password>"
}
```

建议字段约束：secret 43–128 字符、email 3–320 字符（trim/lower）、display name 1–160 字符（trim 后非空）、initial password 12–1024 字符。长度下限属于本稿实现建议，需随规格批准。

`201` 返回 `id/email/display_name/operator_permissions`，不返回 password、password hash、secret 或 JWT。首次 Operator 获得且仅获得 `identity_admin` 与 `registry_operator`；不自动授予 Region Grant。任务权限仍需单独授予 `CN_EAST/read`。

错误：`403 BOOTSTRAP_UNAVAILABLE`（secret 无效）、`410 BOOTSTRAP_EXPIRED`、`409 BOOTSTRAP_CONSUMED`、`409 EMAIL_ALREADY_EXISTS`、`409 BOOTSTRAP_ALREADY_INITIALIZED`、`503` 暂时性数据库失败。代理层对 Bootstrap URI 拒绝请求时返回 404。失败响应不区分或回显任何 secret 值。

状态转换：

```text
UNINITIALIZED --本机签发 secret--> READY
READY --过期--> EXPIRED --本机轮换（未消费）--> READY
READY --完整事务提交--> CONSUMED
READY --请求校验/重复 email/数据库错误回滚--> READY
CONSUMED --任何重放--> CONSUMED + 409
```

过期判断使用数据库时钟。建议默认 TTL 为 15 分钟；固定长度 32 random bytes，URL-safe 编码后一次显示。以上数值和 TTY 输出流程是本稿实现建议，需随设计批准。

### 5.2 Bootstrap 事务与并发

1. 请求先做大小/Schema 检查，并用无锁只读查询预检 digest 与过期时间；以 `hmac.compare_digest` 比较 SHA-256 digest。无效 secret 不触发 Argon2。
2. 对有效请求在 AnyIO worker thread 按现有 `PasswordHasher` 参数生成 Argon2id；不在 asyncio event loop 或持锁事务内做哈希。
3. 开启 PostgreSQL 事务，`SELECT ... FOR UPDATE` 锁唯一状态行；重新比较 digest、数据库过期时间和 `consumed_at`，并确认 `operator_permissions` 尚无记录。
4. 在同一事务插入 active 用户、两条 Operator 权限、`identity.bootstrap.completed` 审计事件；最后设置 `consumed_at/consumed_by` 并提交。
5. 并发请求在行锁后重读状态。只有一个请求可提交；其余返回 409。任意失败回滚用户、权限、审计与消费标记。
6. 重复 email 返回 409 且不消费 secret；管理员查询确认。提交后响应丢失时不重放 secret/响应，也不重建账号；通过本机身份/数据库运维查询确认状态。

建议由本机 `agentforge-bootstrap-secret` 命令生成 secret，连接 migrator，仅将 digest 与 DB 过期时间写入表，默认只在 TTY 一次输出原文；API 服务不接收 secret 环境变量。若 secret 丢失或过期，未消费状态可以本机轮换；已消费状态不可重置。

### 5.3 登录与权限验证

Operator 使用既有 `POST /api/v1/auth/login` 和 `GET /api/v1/auth/me`。JWT 仍只含 subject、iat、exp、token_version；不放权限快照。`/auth/me` 增加当前 `operator_permissions` 字段。

`GET /api/v1/auth/me` 的响应保留现有字段并增加当前权限；普通用户返回空权限数组：

```json
{
  "id": "<uuid>",
  "email": "operator@example.com",
  "display_name": "Initial Operator",
  "regions": [],
  "operator_permissions": ["identity_admin", "registry_operator"]
}
```

每个管理请求在数据库实时查询当前权限：

- Identity 路由精确要求 `identity_admin`。
- Registry 写路由精确要求 `registry_operator`。
- 任何一项不蕴含另一项；缺失时 403 并审计 `operator.authorization` denied。
- Operator 身份本身不绕过 Task ownership 或 Region Grant；仅有 Operator 权限且无 `CN_EAST/read` 的用户不能读取/创建华东 Task。
- 用户创建固定为 `active`；没有禁用、删除、Operator 权限增删端点。

### 5.4 Identity 管理请求

`POST /api/v1/admin/identity/users`（只要求 `identity_admin`）：

```json
{
  "email": "analyst@example.com",
  "display_name": "Analyst",
  "initial_password": "<delivered out of band>",
  "region_grant": {"region_code": "CN_EAST", "permission": "read"}
}
```

`region_grant` 可省略/null。成功 `201` 返回用户 ID、规范化 email、display name、status 与当前 Region Grant；不返回 password/hash。唯一约束是最终并发裁决，冲突映射 `409 EMAIL_ALREADY_EXISTS`。不要求 `Idempotency-Key`，也不缓存完整响应。

管理员可用 `GET /api/v1/admin/identity/users?email=<normalized-email>` 确认重复创建；无匹配返回空列表。Grant 使用 `PUT /api/v1/admin/identity/users/{user_id}/region-grants`，body 只接受 `CN_EAST/read`；撤销使用 `DELETE /api/v1/admin/identity/users/{user_id}/region-grants/CN_EAST`，只设置 `revoked_at` 并追加撤销审计。未知用户 404；任何其它地区/权限 403 `REGION_ACCESS_DENIED`。

密码在 API 侧用现有 Argon2id 参数哈希；哈希在事务外计算。初始密码仅在 TLS 管理连接上传输或本机 loopback 使用；从不进入 response、日志、异常文本或 audit metadata。API 只审计用户/Grant ID、操作、地区和权限，不记录 email 或请求体。

### 5.5 Registry 管理请求

新增认证管理路由，对应现有 CLI 写命令：

- `POST /api/v1/admin/registry/model-profiles`
- `POST /api/v1/admin/registry/tools`
- `POST /api/v1/admin/registry/agents`
- `PUT /api/v1/admin/registry/agents/{version_id}/tools/{tool_id}`
- `POST /api/v1/admin/registry/agents/{version_id}/publish`
- `POST /api/v1/admin/registry/agents/{version_id}/retire`

写入 body 沿用 CLI 字段名，JSON 字段直接传对象；所有模型 `extra='forbid'`：

```jsonc
// model profile
{"profile_key":"main","provider":"fake","model_name":"fake-v1","endpoint_alias":"fake-local","credential_ref":"secret://agentforge/provider","timeout_ms":2500,"max_output_tokens":768,"config_json":{}}
// Agent draft; created_by is deliberately absent
{"agent_key":"sales-report","version":"1.0.0","name":"Sales report","graph_key":"metrics-v1","model_profile":"main","config_json":{}}
// tool version
{"tool_key":"metrics.query","version":"1","input_schema":{"type":"object"},"output_schema":{"type":"object"},"policy_json":{},"execution_kind":"internal","timeout_ms":1000}
// binding
{"policy_override":{}}
```

前四种创建/绑定返回资源 ID 与状态；publish/retire 无 body，成功返回资源 ID、状态与时间（若适用）。客户端 `actor_id`、`created_by` 一律不接受；Agent 创建由服务端写入 `created_by`。

CLI 继续保留只读查询，但 `register/create/bind/publish/retire` 均改为 HTTP client，使用 `AGENTFORGE_API_URL` 与由 Operator 登录取得的 `AGENTFORGE_API_TOKEN`；不再以 Registry DB DML 写入。CLI 不再接受 `--actor-id` 或 `--created-by`；Agent 的 `created_by` 和生命周期审计 actor 均取服务端 principal。

发布、退役仍使用 DB 行锁、现有校验、配置快照与不可变触发器。409 对应唯一键/非法状态冲突；权限不足为 403。Registry 审计 metadata 只含资源 ID、key/version、动作与决策，不含配置 JSON、凭据引用明文、密码、secret 或 JWT。

## 6. 日志与审计安全不变量

- 原文 Bootstrap secret 与用户密码只经调用方/请求处理传输和内存；数据库只存 Argon2id 密码哈希与 Bootstrap SHA-256 digest。既有 Web 客户端会把 JWT 保存在当前 tab 的 `sessionStorage`。三者均不得进入日志、span 属性或 audit metadata。
- 请求体、Authorization header、响应体不新增到 `log_event`、OTel span、错误详情、exception 文本或审计 metadata。
- Bootstrap/登录只记录 reason code、request id 与决策；禁止记录 email/password/secret/token。
- Identity 审计记录操作人与资源 ID、permission、region_code；Registry 审计只记录资源 ID/key/version；不记录原始配置或凭据值。
- 对 API 日志与 audit rows 做 canary 检查：请求中放入唯一测试字符串，确保成功、拒绝、参数校验失败和并发冲突各路径都找不到该字符串。

## 7. P0 认证与任务授权的不兼容风险

1. **P0 Region Grant 比 P0.5 宽**：旧 DDL 接受任意地区和 `admin`；API/Worker 多处将 `admin` 当成读取授权。必须同时收窄管理端点、DDL constraint、API Task/详情/SSE、Evaluation 和 Worker live check，否则旧权限仍可访问数据。
2. **已有数据迁移可能 fail closed**：0013 预检会拒绝存在的活跃非 `CN_EAST/read` Grant 的数据库。当前仓库种子只给 `svc-eval` `CN_EAST/read`；真实部署数据库是否有其它活跃 Grant 未知，必须在迁移前只读核查，未获批准不得自动撤权。
3. **P0 测试假设冲突**：`test_identity_auth.py` 使用 `east/west`，`test_api_tasks.py` 明确测试 `CN_NORTH` 授权后可创建多区域请求。迁移后这些写入或预期会失败；需把测试改为验证 `CN_EAST/read` 成功、其他授权被拒绝。Business 数据仍有 `CN_NORTH/CN_SOUTH`，但无用户 Grant 时必须不可见。
4. **既存 Task**：已持久化 `region_scope_json` 若含其它地区，live revalidation 会拒绝结果/事件访问，Worker 下一次 Tool 调用也会拒绝。迁移前还需只读检查非终态 Task 的 scope；是否等任务结束或由人工取消/失败处理，需单独批准。
5. **JWT 不携带角色**：这是兼容优势；不得把 Operator 权限塞进 JWT，否则数据库权限调整无法即时生效。新增 `/auth/me` 字段仅供显示，不作为鉴权来源。
6. **P0 登录限流缺口**：当前限流在认证成功后才执行，错误密码尝试没有用户级限流。P0.5 不应顺手扩大认证改造；Bootstrap 通过本机边界与高熵一次性 secret 降低暴露。若要求登录 IP 限流，作为独立批准范围。

## 8. Bootstrap 可信访问边界与 Compose 风险

当前 `${AGENTFORGE_API_PORT:-8000}:8000` 默认发布到所有宿主机接口；Web 的 `${AGENTFORGE_WEB_PORT:-3000}:8080` 也相同，Nginx 会代理所有 `/api/`。因此新 Bootstrap API 若直接照此部署，会同时暴露在宿主机网络接口和 Web 入口。

实现前建议：

1. API 宿主机映射固定绑定 `127.0.0.1`，例如 `127.0.0.1:${AGENTFORGE_API_PORT:-8000}:8000`；容器内 `0.0.0.0:8000` 仅用于 Compose 网络通信。
2. Web Nginx 对 `/api/v1/bootstrap` 与 `/api/v1/bootstrap/` 显式返回 404，再进入通用 `/api/` proxy；不能信任可伪造的 `X-Forwarded-For` 或 Host header 判本机。
3. 本机 bootstrap 调用直接请求 `http://127.0.0.1:<port>`。默认 Web/API 只用于本机开发；远程管理必须经 TLS 入口，且不应发布明文 HTTP 的管理端点。
4. Compose 同一 bridge 网络内的容器仍能直接访问 `api:8000`。本设计将当前 Compose 项目内的受控容器视为可信部署边界；若要求“只有宿主机进程可访问，其他容器也不行”，需要另设隔离 listener/network 或 host-side proxy，单靠端口 loopback 映射不够。该边界需在编码前确认。
5. PostgreSQL 当前没有 host `ports`，维持不暴露。启用 observability profile 时匿名 Grafana 端口仍是单独的宿主机暴露面。

## 9. P0.5-A 文件级变更清单（预期，不代表本轮已修改）

| 文件 | 预期变化 |
|---|---|
| `migrations/versions/0013_identity_operator_bootstrap_management_api.sql` | 新建 Bootstrap 状态、Operator 权限、Grant 限制、最小权限 grants、Registry actor 审计变更 |
| `packages/core/src/agentforge_core/schema_version.py` | Schema 版本 12 → 13 |
| `packages/core/src/agentforge_core/bootstrap.py`（新） | 本机生成/轮换 Bootstrap secret，只存 digest，TTY 一次输出 |
| `packages/core/pyproject.toml` | 注册 `agentforge-bootstrap-secret`；Registry CLI HTTP client 所需依赖（若采用 httpx） |
| `apps/api/src/agentforge_api/auth.py` | 复用 Argon2 参数新增异步哈希 helper；基于已验证 principal 实时检查 Operator 权限 |
| `apps/api/src/agentforge_api/identity_admin.py`（新） | Bootstrap、用户创建/查询、Region Grant 授予/撤销 |
| `apps/api/src/agentforge_api/registry_admin.py`（新） | Operator 认证的 Registry 写 API、服务端 actor 与审计 |
| `apps/api/src/agentforge_api/app.py` | 装配新 router；`/auth/me` 返回实时 Operator 权限；Bootstrap 路由和错误处理 |
| `apps/api/src/agentforge_api/tasks.py` | 收窄 Task 创建、详情与 SSE live grant 查询到 `CN_EAST/read` |
| `apps/api/src/agentforge_api/evaluations.py` | service grant 查询显式只接受 `CN_EAST/read` |
| `apps/worker/src/agentforge_worker/gateway_backend.py` | Worker Tool Gateway live grant 检查收窄到 `CN_EAST/read` |
| `packages/core/src/agentforge_core/registry.py` | 写子命令改为认证 HTTP 请求；移除 `--actor-id/--created-by`，只读命令保留只读 DB 权限 |
| `compose.yaml` | API 与 Web 默认绑定宿主机 loopback，远程访问交给 TLS 入口 |
| `apps/web/default.conf.template` | 阻断 Bootstrap 精确路径及尾斜杠路径，不经反向代理 |
| `docs/api.md`、`docs/runbook.md`、`README.md` | 更新 P0.5 API、secret 生命周期、Registry CLI 认证和端口边界 |
| `tests/integration/test_bootstrap.py`（新） | Bootstrap 状态、回滚、成功、错误与并发唯一消费 |
| `tests/integration/test_identity_admin.py`（新） | 用户唯一邮箱、查询确认、区域 Grant 和权限隔离 |
| `tests/integration/test_registry_admin_api.py`（新） | API DML、Actor 来源、审计、不可变性与 CLI HTTP 写路径 |
| `tests/integration/test_migration_0013.py`（新） | Schema、约束、角色权限和活跃 Grant 预检 |
| `tests/integration/test_identity_auth.py` | Operator 登录字段与 Region Grant 夹具更新 |
| `tests/integration/test_api_tasks.py` 及 Worker 授权相关测试 | `CN_NORTH` 多区域成功用例改为 P0.5 拒绝契约；保留 CN_EAST P0 路径 |
| `tests/integration/test_registry_cli.py` | 不再依赖 Registry CLI DML，改验 HTTP 管理 API 与只读 DB 权限 |
| `tests/integration/test_api_readiness.py` | 断言迁移版本 13 |

## 10. 单元、集成、并发与安全测试矩阵

| 层级 | 场景 | 必须观察到的结果 |
|---|---|---|
| 单元 | Argon2 helper 成功/错误哈希；JWT Claims | 密码可验证；参数沿用现有值；JWT 仍 ≤900 秒且不含 Operator 权限 |
| 单元 | Operator permission checker | `identity_admin` 与 `registry_operator` 独立；任一单项不能调用另一类路由 |
| 单元 | Schema 校验 | 多余 `actor_id`、错误地区/权限被拒绝；错误体不回显请求值 |
| 集成 | Bootstrap 有效 secret | 建立一个 active 用户、恰好两条 Operator permission、无默认 Region Grant；返回不含密码/JWT |
| 集成 | Bootstrap 无效/过期/已消费/已初始化 | 对应 403/410/409；无账号或权限副作用；审计只有原因码与 request id |
| 集成 | 重复 email | 409；secret 仍可用；identity_admin 查询能返回现有记录；响应不含 password hash |
| 集成 | 事务故障注入 | 用户/权限/审计/consumed 标记全回滚；相同 secret 在修复数据库后仍可用 |
| 并发 | 同 secret、不同 email 的并发 Bootstrap | 恰好一个 201、其余 409；仅一个新用户、两条权限、一条成功审计；消费人一致 |
| 并发 | 同 email 并发 provisioning | 恰好一个创建；其余 409；唯一约束为最终裁决，无重复用户 |
| 并发 | 两个 Registry publish/retire 请求 | 行锁和状态机仍保证唯一合法转移；每次成功转移仅一条正确 actor 审计 |
| 集成 | 身份权限矩阵 | identity-only 可 provisioning 但不能写 Registry；registry-only 可写 Registry 但不能 provisioning；双权限 Operator 两者可用 |
| 集成 | Task 授权 | Operator 权限不能绕过 ownership/Region Grant；无 CN_EAST/read 时 Task、SSE、结果、Tool Gateway 均拒绝 |
| 集成 | Region Grant 数据库边界 | 仅活跃 CN_EAST/read 可写；活跃其它地区/permission 被约束拒绝；撤销历史行保留 |
| 集成 | Registry DB 角色 | registry_cli 四表 SELECT 成功、DML 失败；agentforge_api 只能执行批准的 INSERT/Agent 状态列 UPDATE |
| 安全 | 传入 canary secret/password/JWT | 成功、拒绝、校验错误、唯一冲突和并发路径的日志、span 属性、audit metadata 均无 canary |
| 安全 | Bootstrap 网络入口 | Host loopback 可达；Web proxy 两种路径均 404；非 loopback 宿主机端口不能连接；Compose 内部访问边界按批准部署配置验证 |
| 迁移 | 0013 预检 | 活跃非 CN_EAST/read Grant 时迁移原子失败；已撤销历史 Grant 不丢失；Ready 只接受版本 13 |

## 11. GO / NO-GO

- **设计审查：条件 GO**。现有 Argon2id、JWT、PostgreSQL 事务、审计表与发布不可变触发器足以承载本设计；没有必要改写认证协议。
- **编码：NO-GO，等待人工批准本规格**。数值 TTL、Bootstrap 生成/轮换操作、本机信任边界、Registry CLI token 输入方式与 API 路由契约属于本稿明确列出的实现选择。
- **迁移/部署：NO-GO**。未核实目标数据库活跃非 CN_EAST/read Grants 与非终态多区域 Tasks；当前 Compose 又会把 API/Web 发布到所有宿主机接口。
- **本轮验证**：只做静态仓库审阅；未运行测试、Compose、迁移或任何账号/密钥操作。

## 12. 实施前仍需人工批准的操作

1. 批准本规格作为 P0.5-A 编码契约；重点确认建议 Bootstrap TTL 15 分钟、secret 32 bytes 与 TTY 一次输出、secret 未消费时的本机轮换、初始密码 12–1024 字符、首个 Operator 不自动获 `CN_EAST/read`、Registry CLI 从 `AGENTFORGE_API_TOKEN` 读取 bearer token。
2. 批准 Compose 默认 API/Web 只绑定宿主机 loopback，并确认受控 Compose 容器网络属于可信本机边界；若不接受，先确定隔离 listener/network 或 host-side proxy 设计。
3. 在任何迁移前，只读核查目标库活跃非 `CN_EAST/read` Grants 和非终态 Task scope。若有命中，人工决定撤销 Grant、等待任务结束或停止任务；0013 不自动修复数据。
4. 实际启用后由人本机运行 secret 生成命令、通过安全渠道交付初始 Operator 凭据并发起 loopback Bootstrap；本轮没有执行这些动作。
5. 远程管理部署先批准 TLS 终止点与管理流量路径；不允许用当前明文 HTTP 端口交付初始密码。
