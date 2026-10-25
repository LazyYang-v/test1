# 03: 冻结数据集与 GMV 定义

**What to build:** 确定性业务数据集生成器：固定随机种子、固定生成器版本、规范哈希算法与排序规则，产出 3 区域 × 180 天 × 日均约 200 单的订单数据，内嵌经营分析全部 fixture。内容哈希即 data_snapshot_id。GMV v1 按冻结口径注册进 metric_definitions，经授权视图以只读角色可查。

**Blocked by:** 01

**Status:** resolved

- [x] 生成器两次运行产出逐字节相同的数据集与相同的 SHA-256 内容哈希（data_snapshot_id 可复现）
- [x] 数据集包含：约 3% 退款单、零基线窗口、华东环比精确 −18% 窗口、未支付与已取消单、跨月跨时区边界数据
- [x] 订单状态 ∈ {pending, paid, refunded, cancelled}；paid_at 仅支付成功时写入、退款后保留
- [x] GMV 按冻结口径（paid_at ∈ [start, end) 的订单 paid_amount 合计，退款不扣除）在授权视图上可查且数值正确
- [x] 未支付/已取消订单不计入 GMV；已退款订单计入（BA-06/07 数据层验证）
- [x] business_ro 角色对业务表无任何写权限，仅能 SELECT 授权视图

**Resolution:** Merged into `integration/p0` at `3dcbf8e`.

**Verification:** Full Offline + PostgreSQL Integration suite passed: 41 passed. `data_snapshot_id`: `e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8`.
