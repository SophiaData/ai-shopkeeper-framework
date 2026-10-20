# Phase 1 本地验收

> 给仓库所有者在自己机器上核对卖家工作流。自动门是文末的 `pytest`；下面的浏览器步骤由你执行。
> 本阶段不实现上架、发货、付款、退款的写接口。上架仍在 TikTok Shop 卖家中心由你本人提交。

## 1. 启动

在仓库根目录：

```bash
cp .env.example .env
make dev
```

浏览器打开 http://127.0.0.1:8000/workflow 。进程默认只听 `127.0.0.1`。

## 2. 离线快照

三个 TikTok 变量都留空：`TIKTOK_APP_KEY`、`TIKTOK_APP_SECRET`、`TIKTOK_ACCESS_TOKEN`。

页面状态应显示 TikTok 为 offline。订单、商品、库存都是空的，并说明没有去拉店铺数据。空列表不是一份编出来的订单。

## 3. 可选：三个凭据与文案提供方

三个变量都填上之后，状态可以变成 live，快照来自 TikTok Shop 只读接口。缺任何一个仍保持离线空列表。刷新令牌 `TIKTOK_REFRESH_TOKEN` 可选。

`WORKFLOW_LLM_PROVIDER` 保持 `mock` 时，草稿来源是本地模板，并标明 mock。改成 `openai`、`coze` 或 `dify` 但地址或密钥为空时，退回同一套模板并标明已降级，不把模板说成模型输出。本阶段不要求你接通真实模型。

## 4. 草稿

用「上架文案」生成一条只含你核对过的尺寸和材质的草稿。质检通过后可以点通过。通过之后草稿状态变为已通过，页面上没有因此上架。

再生成一条正文含「100%防水」的草稿。它应停在待修改，点通过会被拒绝。

## 5. 发货、退款仍拒绝；上架见阶段 3 闸门

发货与退款对任意订单仍返回 403，文案指向卖家中心。listing 的上架改为审批闸门：未通过 → 409；`WORKFLOW_PLATFORM=mock` 且已通过 → 可拿到 mock 平台商品 ID。步骤与真店人工确认见 [阶段 3 验收](./phase3-m2-listing-acceptance.md)。

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/workflow/orders/1/ship
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/workflow/orders/1/refund
```

## 6. 报表

`RESEARCH_INPUT_PATH` 和 `DAILY_METRICS_PATH` 留空，或指向一个不存在的文件时，工作台里的「现在跑选品摘要」和「现在跑日报」会写明缺文件，正文包含「不编造」，不出现示例商品或示例数字。

把这两个变量指到 `scripts/fixtures/candidates.example.csv` 和 `scripts/fixtures/metrics.example.csv` 后，两份报表应能生成，并写明这是仓库自带的示例数据，不能当成选品结论或店铺成绩。

## 7. 自动门

```bash
make check
PYTHONPATH=src python3 -m pytest -m "not real"
```

`tests/workflow/test_seller_e2e.py` 覆盖上面的生成、质检失败不能通过、通过、403，以及缺 CSV 不编造和示例 CSV 能生成报表。它使用 TestClient 和 mock，不访问外网。带 `real` 标记的用例不在这条命令里。
