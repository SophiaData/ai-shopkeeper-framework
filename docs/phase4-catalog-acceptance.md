# 商品库最小页验收

> 给仓库所有者核对「本地 listing / 商品一览：审批状态 + 平台商品 ID」与「最小本地编辑」。
> Done 条件的家是 [DESIGN.md](./DESIGN.md) §9 M2。真写须另开 `tiktok_live` 环境闸门，见 [phase5-real-listing-acceptance.md](./phase5-real-listing-acceptance.md)。

## 1. 交付了什么

| 能力 | 怎么验收 |
|---|---|
| 列表 | `GET /api/workflow/catalog` 含 `approval_status`、`platform_product_id`（及本地 `price_amount` / `currency` / `specs`） |
| 闸门写入 | mock 下 approve → publish 后，商品库出现对应行且带 `mock-pp-…` |
| Demo 导入 | `POST /api/workflow/catalog/import-demo`（或工作台按钮）扫描 `artifacts/demo/packages/listing_*/listing.json`；`packages_dir` 必须落在 `artifacts/demo` 内（防路径穿越） |
| 本地编辑 | `PATCH` 或 `PUT /api/workflow/catalog/{id}` 可改 `title` / `price_amount` / `currency` / `specs`；不外呼平台、不改已发布平台商品 |
| 再审闸门 | 编辑后 `approval_status`→`pending`（关联 listing 草稿同步回 pending）；未审批 `publish`→409；重新 approve 后可再 publish（mock） |
| 页面 | `/workflow`「商品库」区块带「编辑」表单；相邻入口 `/workflow/catalog` |
| 硬边界 | 发货/退款仍 403；不打开真实 TikTok 写 |

## 2. 手动路径

```bash
export WORKFLOW_PLATFORM=mock
export WORKFLOW_LLM_PROVIDER=mock
make demo          # 先有交付包
make dev
```

浏览器打开 http://127.0.0.1:8000/workflow#catalog （或 `/workflow/catalog`）：

1. 点「从 Demo 交付包导入」→ 出现 `approval_status=demo` 的行。
2. 生成 listing 草稿 → 通过 → 请求上架 → 商品库该行变为 `published` 且有平台商品 ID。
3. 点「编辑」改标题/价格 → 保存 → 该行回 `pending`；草稿须重新通过后才能再请求上架。已发布平台商品不会被本编辑改写。

## 3. 自动门

```bash
PYTHONPATH=src python3 -m pytest -m "not real" tests/workflow/test_catalog.py
```

覆盖：列表/导入/路径穿越；编辑后 pending；未审批不能 publish；重新批准后可 publish；页面含编辑控件。全部走 mock。
