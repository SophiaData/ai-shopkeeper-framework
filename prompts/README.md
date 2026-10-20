# 通用提示词示例（example）

这里的七份文件是供人工试用的中性模板，所有商品、指标和政策必须由使用者提供。它们没有真实店铺经营话术、买家消息、销售成绩或平台凭据，也不是已接通的 Coze / Dify 流程。现有应用的默认站点画像、泰国站家居收纳类目判断和 THB 入库限制仍如 [DESIGN.md](../docs/DESIGN.md) 所述；模板通用不表示运行时代码支持多站点。

| 模板 | 产出 |
|---|---|
| [product-research.md](./product-research.md) | 合成商品候选摘要 |
| [listing-copy.md](./listing-copy.md) | 带证据标记的 listing 草稿 |
| [short-video-script.md](./short-video-script.md) | 依据已核实卖点的拍摄脚本 |
| [influencer-outreach.md](./influencer-outreach.md) | 未发送的合作邀约草稿 |
| [customer-service.md](./customer-service.md) | 人工审查的客服回复草稿 |
| [faq-seed.md](./faq-seed.md) | 待补政策的合成 FAQ |
| [daily-report.md](./daily-report.md) | 缺数显式保留的日报草稿 |

人工在自己使用的工具中贴入「系统提示」块，并提供自造输入验证格式。不要输入密码、Cookie、真实买家 PII、订单号、私有费率或其他店铺秘密。模板不会直接执行上架、付款、发货、退款或消息发送；有外部副作用的动作依应用审批与操作权限处理。使用顺序见 [example 起步清单](../docs/week-1-starter.md)。

编排器的达人步骤在文件存在时读取 [influencer-outreach.md](./influencer-outreach.md) 的 fenced `text` 系统提示，同时保留 `outreach.py` 内置安全约束与 JSON 输出契约。其余文件供人工使用；短视频步骤以代码内结构生成，不解析短视频 Markdown。
