# 示例：本地 mock 商店工作流（example）

> 本文件是公开框架的合成示例，不是店铺运营手册；路径保留供现有文档链接与测试使用。没有真实店铺、订单、买家、销售成绩或供应商策略。产品边界见 [DESIGN.md](./DESIGN.md)，数据边界见 [SCHEMA.md](./SCHEMA.md)。

此仓库当前以 TikTok Shop Thailand 和 THB satang 为默认实现示例，`site_profile` 的已登记画像只有 `th-home`。商品规则也仍包含泰国站家居收纳类目。这些是现有代码的示例假设，不代表已实现任意站点、任意币种或通用平台写入。完整的本地启动与默认锁定状态见 [README.md](../README.md)。

## 运行合成演示

```bash
python -m pip install -e '.[dev]'
python -m shopkeeper.cli demo-orch
```

`demo-orch` 强制 mock 货源、平台与 LLM，生成选品、listing、短视频脚本、达人建联草稿、审批和后续模拟步骤的本地产物。输出目录是 `artifacts/demo-orch/`，不应提交真实业务数据。`TTS_WRITE_ENABLED=0` 仍为默认；演示的商品 ID、订单与审批都是合成结果，不是卖家中心数据。需要自己读一组稳定、无个人信息的订单/listing/审批形状时，见 [synthetic-workflow.json](../examples/synthetic-workflow.json)；该 JSON 只供阅读，不能作为平台 API 请求直接导入。

## 规则与人工边界

- 上架、改价、下单、投放等有外部副作用的动作必须经过 `approvals`；生成草稿与只读请求不触发该闸门。
- 发货、退款、付款的应用入口固定拒绝；mock 演示不构成已连接真实平台的证明。
- 不把任何真实密钥、买家信息、订单号或商业费率塞进示例或提示词；使用本仓库的 [通用 prompt 示例](../prompts/README.md) 时先替换自己的政策，并在人审之后才使用输出。
- 平台规则、价带、可用接口与实际店铺权限需要独立确认。本示例不声称实现多站点经营。

选品报告的可执行类目判断在 `src/shopkeeper/scout/category_rules.py`：价带为本仓库整理的参考区间，非抓取的竞品实测数据；偏离只作提醒，不阻断流程。没有零售价时为 `unknown`；1688 人民币成本不得充当 THB 零售价。示例 prompt 不复刻这张业务价带表，具体行为以代码和聚焦测试为准。

如果要依次看本地入口和验收命令，见 [example 起步清单](./week-1-starter.md)。
