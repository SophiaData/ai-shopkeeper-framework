# 阶段 5：真写上架（tiktok_live）验收

> 给仓库所有者核对「已审批 listing → 可选真写 Open API → 平台商品 ID 进商品库」。
> Done 条件的家是 [DESIGN.md](./DESIGN.md) §9 M2。发货、退款、付款仍固定拒绝。
> **默认关闭。** 未同时满足环境放行条件时，行为与只读客户端相同：`TTS_WRITE_BLOCKED`。

## 1. 凭据与权限

| 项 | 要求 |
|---|---|
| Open API 凭据 | `TIKTOK_APP_KEY` / `TIKTOK_APP_SECRET` / `TIKTOK_ACCESS_TOKEN`（可选 refresh / expire） |
| 店铺 | `TIKTOK_SHOP_CIPHER` 必须是**本店** cipher；区域建议 `TIKTOK_SHOP_REGION=TH` |
| 开放平台权限 | Partner Center 已开通商品创建 / 图片上传 / 类目属性等写范围；沙箱与生产勿混用 |
| 本机密钥 | 只写在 `.env`（已 gitignore）。禁止提交到 Git、文档、截图或审计日志 |

软件侧额外硬闸门（与审批闸门叠加）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `WORKFLOW_PLATFORM` | （空） | 须为 `tiktok_live` |
| `TTS_WRITE_ENABLED` | `0` | 须为 `1` 才允许外呼写 |
| `TTS_WRITE_SHOP_ALLOWLIST` | 空 | 逗号分隔的 `shop_cipher` 白名单；当前 cipher 必须命中 |
| `TTS_WRITE_DAILY_LIMIT` | `1` | UTC 日成功 `create_product` 上限 |
| `TTS_WRITE_MAX_PER_REQUEST` | `1` | 单次 `create_listing` 创建商品数上限（实现固定建 1 个） |
| `TTS_WRITE_AUDIT_PATH` | `artifacts/tts_write_audit.jsonl` | 写操作审计（不含密钥） |

未审批的 listing 点「请求上架」仍是 HTTP 409，且**不会**调用适配器。

## 2. 演练 → 批准 → 真写 → 关写（推荐顺序）

真写只有一次机会（日上限 1），所以**先干跑再开写**。四步走完约 20 分钟。

### 步骤 A：干跑（不外呼任何写接口）

`tiktok_dry_run` 会走完整的载荷构造与校验，但**不调用 create_product**：

```bash
# .env
WORKFLOW_PLATFORM=tiktok_dry_run
TTS_WRITE_ENABLED=0          # 干跑不需要开写
```

在工作台里：生成草稿 → 补上架必需字段（类目/仓库/价格/主图）→ 通过审批 → 上架。
预期返回 `dry_run: true`、`status: dry_run`，且**没有任何外呼**。

干跑能提前暴露的问题：类目必填属性没填、主图缺失、价格非法、载荷超长。
这些在真写阶段每一个都会烧掉当天唯一配额。

### 步骤 B：owner 书面批准

在本项目的协作渠道留下一条明确批准（谁、何时、哪个 SKU）。
**没有这条记录就不要进入步骤 C**。书面批准开写仍是 owner 事项（交接计划 P0-11）。
发布失败怎么恢复、上架带不带价，已在 [DESIGN §12.1](./DESIGN.md) 按选项 A 关闭，不挡这一步。

### 步骤 C：真写（只开 1 个 SKU）

按下面「开启步骤」设置 `.env`，然后重复步骤 A 的操作。
上架后立刻在工作台「商品库」核对平台商品 ID 与审核状态。

### 步骤 D：立即关写，验收后不要把开关留在 1

成功、失败或中途停下，都要在离开机器前把 `TTS_WRITE_ENABLED` 改回 `0`。
不要为了「下次方便」留在 `1`。Windows 用下面的脚本（仓库根目录）：

```powershell
# 先看现状（只读，不改任何文件）
powershell -File scripts\windows\tts-disarm.ps1

# 确认要关，再执行
powershell -File scripts\windows\tts-disarm.ps1 -Apply
```

脚本**只能把 `TTS_WRITE_ENABLED` 写成 0**，不会碰白名单、日上限或任何密钥；
不带 `-Apply` 时是纯只读报告。关掉后**记得重启工作台**（`.env` 在启动时读取）。
Windows 工作台默认端口是 **8010**（`WORKFLOW_PORT`；8000 常被系统占用）。下文地址按本机实际端口替换。

### 步骤 E：`execute_failed` 之后重新生成审批单

`create_product` 超时或断连之后，失败记在幂等键
`(adapter, create_listing, draft_id, approval_id, payload_hash)` 上，`attempt_no` 恒为 1。
**同一张审批单再点上架会永久回放这条失败记录，不会再次调用平台。**
没有换键按钮，也没有换键 API（[DESIGN §12.1](./DESIGN.md)，2026-10-17 选项 A）。

1. 先打开 TikTok Shop 卖家中心，确认这次超时有没有其实已经建成商品。建成了就不要再发。
2. 没有建成时，在工作台再生成一条 listing 草稿（页面「生成草稿」，或 `POST /api/workflow/drafts/generate`），补上架必需字段，重新审批，再上架。新草稿会得到新的审批单；新的 `approval_id` 才是新的幂等键。
3. 不要在原来那张 `execute_failed` 审批单上反复点「请求上架」指望重试。同一 `approval_id`、载荷不变时，后续请求回放失败记录，平台调用次数不加。
4. 失败不占每日成功配额（只数 `status=ok`）。确认不再发之前，回到步骤 D 关写。

这条路径由 `tests/workflow/test_publish_idempotency_timeout.py` 钉住（同键回放，换新单才能再调平台）。本手册不改变该行为。

### 步骤 F：售价在卖家中心设置或改价

`create_listing` 发给平台的载荷**不含价格字段**。已批准的偏离价只留在摘要里（`approved_price_thb` 与 `price_source` 参与 `payload_digest`），用来事后发现「批准的是 A 价、摘要对不上」。系统不会把这个数字写成平台售价。

到 TikTok Shop 卖家中心自行设置或改价，再按清单第 8 步核对。

## 3. 开启步骤

```bash
# 1) 凭据进 .env（不要 export 到 shell 历史若可避免）
cp .env.example .env
# 编辑 .env：填 TIKTOK_* ，再设：
# WORKFLOW_PLATFORM=tiktok_live
# TTS_WRITE_ENABLED=1
# TTS_WRITE_SHOP_ALLOWLIST=<与 TIKTOK_SHOP_CIPHER 相同的值>
# TTS_WRITE_DAILY_LIMIT=1

# 2) 启动工作台
export WORKFLOW_LLM_PROVIDER=mock
make dev
```

打开 http://127.0.0.1:8010/api/workflow/status （Windows 默认 8010；端口被顺延时以 `.env` 的 `WORKFLOW_PORT` 为准）：

- `platform` = `tiktok_live`
- `publish_enabled` = true
- `tts_write_armed` = true（仅当 ENABLED + 白名单命中）
- `ship_enabled` / `refund_enabled` = false

## 4. 用 1 个 SKU 的验收清单

准备：叶子类目 ID、仓库 ID、一张主图（本地文件转 base64，或在草稿 `input.image_b64` 提供）。

1. 生成一条 listing 草稿。`items[0]` 至少含核对过的 `name` / `specs`，以及：
   - `category_id`（叶子类目）
   - `warehouse_id`
   - `price_amount`（如 `99`）、`currency`（如 `THB`）
   - `image_b64`（主图 JPEG/PNG 的 base64，无数据 URI 前缀）
2. 人工核对泰英文案，质检通过后点「通过草稿」（或 `POST …/approve`）。
3. 未通过前点「请求上架」→ **409**，审计与平台均无写。
4. 通过后再「请求上架」→ **200**：
   - 草稿 `status=published`，`platform_product_id` 非空
   - 商品库出现对应行且带同一平台 ID
   - `artifacts/tts_write_audit.jsonl`（或你配置的路径）出现 `upload_image` / `get_category_attributes` / `create_product`，内容**无** `app_secret` / token
5. 查询审核状态：
   ```bash
   curl -s http://127.0.0.1:8010/api/workflow/products/<platform_product_id>/audit
   ```
   期望含 `audit_status`（如 `AUDITING` / `APPROVED` / `FAILED`）。
6. 故意再请求上架第二条（或同日第二条新草稿）→ 在默认日上限 1 下应失败（`TTS_WRITE_QUOTA_EXCEEDED`），不产生第二个平台商品。
7. 发货 / 退款：
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8010/api/workflow/orders/1/ship
   curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8010/api/workflow/orders/1/refund
   ```
   均为 **403**。
8. 到 TikTok Shop 卖家中心确认该 1 个 SKU 的审核/上架结果。售价在卖家中心设置或改价（见步骤 F）；库存由你本人核对。验收做完立刻回到步骤 D，确认 `TTS_WRITE_ENABLED=0`。

## 5. 回滚方法

按优先级从快到稳：

1. **立即停写**：`.env` 设 `TTS_WRITE_ENABLED=0`（Windows 优先 `scripts\windows\tts-disarm.ps1 -Apply`；或清空 `TTS_WRITE_SHOP_ALLOWLIST`）后重启进程。已发出的平台商品不会自动删除。验收结束不得把开关留在 `1`。
2. **切回只读 / 干跑**：`WORKFLOW_PLATFORM=`（有凭据则只读）或 `tiktok_dry_run` / `mock`。
3. **平台侧**：在卖家中心下架或删除该测试商品；以卖家中心为准。
4. **本地痕迹**：可保留 `tts_write_audit.jsonl` 与 `external_writes` 作审计；不要为了「干净」删掉事故证据后再无法对账。

回滚**不会**撤销已在 TikTok 侧创建的商品；软件只保证关闭后不再外呼写接口。

## 6. 自动门

```bash
PYTHONPATH=src python3 -m pytest -m "not real" tests/workflow/test_tiktok_live_gate.py
```

覆盖：未放行 / 白名单拒绝 → `TTS_WRITE_BLOCKED`；日上限；未审批 409；放行后 mock HTTP 走通并写入商品库；发货退款 403。不访问外网、不需要真实密钥。
