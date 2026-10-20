# n8n 提醒存根

这里有两条**默认不激活**的工作流，对应执行手册 [§2.6](../docs/thailand-home-workflow.md) 和 [§2.7](../docs/thailand-home-workflow.md) 的每日提醒。第 1 周的导入顺序见 [week-1-starter.md](../docs/week-1-starter.md)。

| 文件 | 作用 |
|---|---|
| [morning-ops-reminder.json](./morning-ops-reminder.json) | 工作日 08:30（`Asia/Bangkok`）生成一张「去卖家中心看发货和库存」的清单 |
| [evening-review-reminder.json](./evening-review-reminder.json) | 工作日 21:00 提醒复盘；另有一个 Webhook，供你把已导出的指标 JSON 贴进来做回执 |

## 导入

1. 打开你自己的 n8n（本机常见地址是 `http://127.0.0.1:5678`，以你的安装为准）。
2. 进入 Workflows → Import from File，选择上面的 JSON。
3. 导入后确认工作流是 **Inactive**。JSON 里 `"active": false`。
4. 打开工作流设置，确认时区是 `Asia/Bangkok`。你人在国内、希望按上海时间提醒时，改工作流时区或改 Cron，并在日历记下来。
5. 先点 Execute workflow 手动跑一次，看 Set 节点里的中文清单。
6. 确认清单内容符合你的作息后，再由你自行激活。

## 这些存根故意没有的东西

- 没有卖家中心账号、Cookie、App Secret 或任何凭据。
- 没有 HTTP Request 节点，因此不会登录或抓取 TikTok。
- 没有飞书、邮件或 Slack 节点。你要接收提醒时，在自己的 n8n 里添加通知节点，凭据只保存在你的 n8n 里。
- 晚上的 Webhook 路径是 `seller-daily-metrics`。激活后它会变成你实例上的一个地址，不要把店铺密钥放进请求体。它只把收到的内容原样交给后面的 Set 节点做提醒，不调用大模型，也不改价。

订单和库存的事实仍然以你登录后的 Seller Center 为准。没有官方 API 时，用卖家中心的导出，再交给 [daily_report.py](../scripts/daily_report.py)。
