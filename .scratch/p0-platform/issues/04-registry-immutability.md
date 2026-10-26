# 04: Registry 与发布不可变性

**What to build:** Registry CLI：注册模型 Profile、创建/验证/发布 Agent 版本。发布时把生效模型配置完整冻结为不可变快照；发布后关键配置的修改被数据库层（触发器/权限）拒绝，CLI 无法绕过。发布与退役产生追加式审计事件。退役只影响新任务解析，不影响已创建任务。

**Blocked by:** 02

**Status:** resolved

- [x] CLI 完成 model_profile 注册与 agent 版本创建、验证、发布全流程
- [x] 发布时生效模型配置（Provider、模型名、参数、超时、Token 限额）被完整复制进不可变快照；密钥仅存 credential_ref 引用
- [x] 对已发布版本关键配置列的 UPDATE 被数据库触发器/权限拒绝（HS-12）；CLI 无法绕过
- [x] policy_override 只能收紧工具策略、不能放宽
- [x] 退役版本阻止新任务解析到它，已创建任务不受影响
- [x] 发布与退役各产生一条审计事件（操作者、动作、资源、决策、时间）

**Resolution:** Merged into `integration/p0` at `0dd3f0e`; Registry function execution is restricted to trusted trigger execution and the CLI role has only Registry table DML plus schema usage.

**Verification:** Focused Registry PostgreSQL integration tests passed (7 passed); after migrations 0001–0005 were integrated, the full suite passed (50 passed).
