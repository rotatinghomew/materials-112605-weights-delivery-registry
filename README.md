# 材料模型权重许可交割与替代适配包

研发联盟按实际权重工件、数据批和客户登记确认交换必要资料。

《权重封装与登记》说明在 [交换约定](docs/exchange.md)。`fixtures/exchange.json` 是本机联调使用的合成记录。项目读取并校验这些交换文件，回放对接方回执，并把权重谱系、发布审查、订单兑现和客户登记串起来。

运行环境为 Python 3.11.2，只依赖标准库。执行 `python3 -m unittest discover -s tests -v` 检查交换资料与业务规则。执行 `python3 partner.py` 后，可通过回环地址的 8087 端口查询回执，`GET /health` 返回服务状态。如需模拟查询暂时失败，启动时传入 `--unavailable`。

## 业务规则

- **谱系与义务**：权重交付义务沿 `parents` 与 `base_id` 传递，包含没有直接读取的上游批次；删掉 `data` 里的引用不能证明义务消失。
- **许可边界**：用途、辖区、生效时间取交集；撤销只作用于对应许可、用途与辖区，内用授权不受地区转授撤销影响。
- **发布审查**：审查员依据送审的具体字节摘要和审查当时已生效的授权批准；批准钉住具体包组成，不采用后来才签的许可。
- **订单兑现**：可接受覆盖相同的替代组合或整套交割；替换必须同时满足材料族覆盖、基座兼容和订单允许替换。不相关的许可变动不会让进行中的订单全部作废。
- **客户登记**：先接收交割包，再按批准辖区确认可取。批准撤销时，已取件的登记收回并停止再次取用；未确认取件且交割响应未知的转为待核实，并保留外方管控可能。
- **双方核对**：交割凭据绑定订单、认领号与具体字节摘要；同一排他包不能因回执先后或双方同时改选而交付两份。
- **最小可见性**：客户可从订单查询可用版本与允许辖区；研发伙伴只能看到与自己数据批有关的交割事实。

## 命令行

```bash
python3 -m exchange validate
python3 -m exchange lineage W3
python3 -m exchange entitlement W3 --use review --zone EU --at 2027-01-18T11:01:00+08:00
python3 -m exchange boundary --weight W3 --at 2027-01-18T11:30:00+08:00
python3 -m exchange trainable W5 --zone EU --at 2027-01-18T11:30:00+08:00
python3 -m exchange audits
python3 -m exchange approvals --at 2027-01-18T11:30:00+08:00
python3 -m exchange plans --at 2027-01-18T11:30:00+08:00
python3 -m exchange versions O1 --at 2027-01-18T11:30:00+08:00
python3 -m exchange check-switch O1 --weight W1 --weight W4 --at 2027-01-18T11:30:00+08:00
python3 -m exchange changes
python3 -m exchange receipts --at 2027-01-18T12:00:00+08:00
python3 -m exchange sync --url http://127.0.0.1:8087 --at 2027-01-18T12:00:00+08:00
```

客户登记库把每次交割凭据和事件写入 JSON：

```bash
python3 -m exchange ledger issue    --ledger ledger.json --order O1 --claim C-1 --weight W1 --weight W2 --at 2027-01-18T10:30:00+08:00
python3 -m exchange ledger unknown  --ledger ledger.json --credential <id> --at 2027-01-18T10:31:00+08:00
python3 -m exchange ledger stage    --ledger ledger.json --credential <id> --at 2027-01-18T10:32:00+08:00
python3 -m exchange ledger enable   --ledger ledger.json --credential <id> --at 2027-01-18T10:33:00+08:00
python3 -m exchange ledger obtain   --ledger ledger.json --credential <id> --at 2027-01-18T10:34:00+08:00
python3 -m exchange ledger revoke   --ledger ledger.json --credential <id> --at 2027-01-18T11:10:00+08:00
python3 -m exchange ledger apply-change --ledger ledger.json --change RL1 --at 2027-01-18T11:10:00+08:00
python3 -m exchange ledger reconcile --ledger ledger.json --credential <id> --digest <sha256> ...
python3 -m exchange ledger risk     --ledger ledger.json
python3 -m exchange ledger partner  --ledger ledger.json --owner N-K
python3 -m exchange ledger customer --ledger ledger.json --order O1
```

所有时间参数都要求带时区偏移；业务判断一律使用 `happened`，`arrived` 只决定该时刻能否看到回执。

## 模块

| 模块 | 职责 |
| --- | --- |
| `exchange/model.py` | 装载、结构/引用/工件字节校验 |
| `exchange/lineage.py` | 谱系、继承义务、许可边界、撤销影响 |
| `exchange/review.py` | 审查批准与生效批准 |
| `exchange/fulfilment.py` | 订单兑现、替代组合、双方条件核对 |
| `exchange/registry.py` | 交割凭据、客户登记库、撤销处置、最小可见性 |
| `exchange/sync.py` | 回执查询与按业务时间合并 |
| `exchange/transport.py` | 向对接方发出交割请求 |
| `exchange/cli.py` | 命令行入口 |
