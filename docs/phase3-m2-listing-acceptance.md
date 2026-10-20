# 阶段 3：M2 上架通道（软件侧）验收

> 给仓库所有者核对「草稿 → 人工通过 → 调用平台写适配器 → 留下平台商品 ID」与「只读订单同步」。
> 真店上架 ≥ 3 个 SKU **不由本软件自动完成**；钱与上架最终以卖家中心人工确认为准。
> Done 条件的家是 [DESIGN.md](./DESIGN.md) §9 M2。完整八态 `approvals` / 飞书卡片仍后置；本阶段是工作台侧薄闸门。

## 1. 本阶段交付了什么

| 能力 | 怎么验收 |
|---|---|
| 审批闸门 | 未通过的 listing 点「请求上架」→ HTTP 409，且 `MockPlatform.create_listing` 调用次数为 0 |
| 审批后上架（mock） | `WORKFLOW_PLATFORM=mock` 下通过后再请求上架 → 200，返回 `platform_product_id` |
| 拒绝/过期 | 已拒绝或超过 7 天未执行 → 409，不调用写适配器 |
| 只读订单同步 | `POST /api/workflow/orders/sync` 写入本机缓存；发货/退款仍 403 |
| 发货 SLA 提醒 | `GET /api/workflow/orders/sla`：临期/超时在 `/workflow` 可见；本地提醒，飞书后置；不自动履约 |
| 真店 | 见第 4 节人工步骤；本 PR 不自动创建平台商品 |

发货、退款、付款固定拒绝。无「跳过本次审批」旁路。

## 2. 一键 mock 全流程

```bash
export WORKFLOW_PLATFORM=mock
export WORKFLOW_LLM_PROVIDER=mock
make dev
```

浏览器打开 http://127.0.0.1:8000/workflow ：

1. 生成一条「上架文案」，事实里只写你核对过的尺寸和材质。
2. 在「审批收件箱」点「通过草稿」。此时**还没有**平台商品 ID。
3. 再点「请求上架」。状态变为 `published`，页面显示 `platform_product_id`（形如 `mock-pp-…`）。
4. 点「只读同步订单」。mock 默认同步空列表；夹具订单见自动测试。
5. 若同步到待发订单且带 `ship_deadline` / `paid_at`，「发货 SLA 提醒」会显示临期或超时；点「刷新发货 SLA」可重算。不会触发发货。

等价 API：

```bash
# 未审批 → 409
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/workflow/drafts/<id>/publish

# 通过后再 publish → 200，body 含 platform_product_id
curl -s -X POST http://127.0.0.1:8000/api/workflow/drafts/<id>/approve -H 'content-type: application/json' -d '{"note":"核对过"}'
curl -s -X POST http://127.0.0.1:8000/api/workflow/drafts/<id>/publish

# 发货/退款仍 403
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/workflow/orders/1/ship
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/workflow/orders/1/refund
```

## 3. 真实凭据下的预检 / 干跑

三个 TikTok 变量都填好后：

```bash
export WORKFLOW_PLATFORM=tiktok_dry_run
make dev
```

- 订单/商品/库存走只读 Open API（与阶段 1 相同）。
- 「请求上架」会调用干跑适配器：校验并留下 `payload_digest`，**不**调用 TikTok 商品创建 API，草稿状态为 `dry_run`，`platform_product_id` 为空。
- 默认 `WORKFLOW_PLATFORM` 为空且有凭据时，客户端仍是只读；`create_listing` 继续 `TTS_WRITE_BLOCKED`。
- `WORKFLOW_PLATFORM=rpa_stub` 仅占位，上架返回 `TTS_RPA_STUB`。

干跑成功不等于店铺里已有商品。真上架仍须你在卖家中心提交。

## 4. 真店 ≥ 3 个 SKU（人工，本 PR 不代执行）

1. 在工作台为 3 个真实商品各生成 listing 草稿，核对泰英标题与描述，去掉幻觉数字。
2. 逐条「通过草稿」。不要跳过质检失败的稿。
3. 可选：`WORKFLOW_PLATFORM=tiktok_dry_run` 对每条「请求上架」，确认干跑成功且无外呼写接口。
4. 打开 TikTok Shop 卖家中心，由你本人创建或提交至少 3 个商品。记下平台商品 ID。
5. 在卖家中心确认商品可见、价格与库存由你本人核对。软件不代点发布、不代收款、不代发货。

真写已另开可选通道：`WORKFLOW_PLATFORM=tiktok_live` + `TTS_WRITE_*` 环境闸门，见 [phase5-real-listing-acceptance.md](./phase5-real-listing-acceptance.md)。本阶段默认仍不把真写打开。

## 5. 自动门

```bash
PYTHONPATH=src python3 -m pytest -m "not real"
```

`tests/workflow/test_m2_listing_gate.py` 覆盖：未审批不调用适配器、审批后留下 `platform_product_id`、拒绝与过期不写、只读订单同步、发货退款仍 403。  
`tests/workflow/test_order_sla.py` 覆盖：超时/临期/窗口内判定、同步后 SLA API、工作台文案、发货仍 403。不访问外网。
