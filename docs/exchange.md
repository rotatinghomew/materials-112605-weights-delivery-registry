# 权重封装与登记

weight 和 adapter 仅为合成工件。sha256 从实际工件字节计算。base_id 为空表示基座，非空说明适配包只支持所给基座。parents 包含参与训练的全部上游检查点，data 只列这次直接输入。批次签署的使用目的与辖区分别限制，internal 仅用于内部训练。一个权重包可有多个覆盖族。登记回执须能比对 order、claim 和 digest。

资料使用 UTF-8 JSON。每个集合各自维护记录 id，跨集合引用使用相应记录 id。事件中的 happened 是业务发生时间，arrived 是对接方可提供该回执的时间。到达时间不能代替业务时间。这些记录来自不同流程阶段，读到一个回执并不代表本地已做过相应操作。

## `batches`

原始数据的权利主张和登记人。字段为 `id`, `material`, `owner`。

## `licenses`

范围是单批、用途与辖区的交集。字段为 `id`, `data`, `uses`, `zones`, `begins`, `expires`。

## `weights`

训练回执含实际数据和接续检查点；file 为工件路径，sha256 为原样字节摘要。字段为 `id`, `base_id`, `parents`, `data`, `coverage`, `file`, `sha256`。

## `orders`

完整交割的材料族，各订单允许的替代范围。字段为 `id`, `receiver`, `zone`, `use`, `families`, `existing_base`, `replace`。

## `audits`

审查记录可回到实际送审工件版本。字段为 `id`, `reviewer`, `order`, `candidates`, `happened`。

## `changes`

原签署方可撤销单项转授地区，内用授权不受该条影响。字段为 `id`, `license`, `use`, `zone`, `happened`。

## `receipts`

登记点回执。digests 对应这一回执指称的实际工件摘要。字段为 `id`, `order`, `claim`, `files`, `digests`, `state`, `happened`, `arrived`。

## 回执查询

本机回放只提供对接方的既有回执。`GET /v1/receipts?at=...` 的 `at` 是 URL 编码后的带偏移时间，返回对象中 `receipts` 是在该时间之前及当时可取得的回执。同一条数据可以在连续查询中出现，列表先后不代表业务先后。`503` 表示当前无法查询回执。
