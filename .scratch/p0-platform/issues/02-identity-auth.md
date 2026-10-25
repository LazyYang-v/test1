# 02: 身份与认证——登录、JWT、区域授权

**What to build:** 用户以邮箱密码登录换取短时效 JWT（HS256，≤15 分钟）；受保护 API 校验 JWT；活跃区域授权（region_grants）决定用户的数据范围；提供当前用户身份与授权的查询端点。权限校验走实时查询，不信任 Token 中的旧权限——这是后续敏感操作实时鉴权的基础。

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] 登录成功返回有效期 ≤15 分钟的 JWT；错误凭据返回 401 UNAUTHENTICATED
- [ ] disabled 用户登录被拒；token_version 变更后旧 JWT 立即失效
- [ ] 无凭证/过期凭证访问业务 API 返回 401，统一错误体（code/message/retryable/request_id）
- [ ] 当前用户端点返回身份与活跃区域授权列表（ revoked 的不出现在内）
- [ ] 撤销区域授权后，实时权限查询立即不再返回该区域（HS-11 的基础）
- [ ] 登录失败与区域权限拒绝产生审计事件
