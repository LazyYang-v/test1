---
status: accepted
---

# 业务数据快照与证据绑定（ADR-009）

P0 的分析结论必须可复现、可核验，因此每个 Task 在创建时绑定一个 Data Snapshot（种子数据集内容哈希，存为 `runtime.tasks.data_snapshot_id`），其全部指标结果与最终报告必须出自同一快照；每次受控指标查询的不可变结果记录为 Evidence（`harness.evidences`），由平台生成，模型只能引用不能生成。Review Validator 校验报告中每个 Evidence 引用存在、归属当前 Task、快照一致且数值相符。

## Considered Options

- 生产级时点查询 / repeatable-read 快照机制——P0 数据集为静态种子数据，不值得引入该基础设施，推迟到 P1 连续数据接入时。
- 信任模型转述的指标数值——被否决：数值、变化率、证据 ID 一律由确定性组件产生或校验。

## Consequences

- 种子数据集内容一变，快照哈希即变；数据集演进必须通过新版本，不允许原地修改。
- 种子数据必须包含退款订单与零基线窗口，否则 BA-03/BA-07 无从测试。
