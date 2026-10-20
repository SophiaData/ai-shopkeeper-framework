# 真 API 只读集成测试

> 家：本文件。默认 CI 与本地确定性门仍是 `pytest -m "not real"`（见 [scripts/ci.ps1](../scripts/ci.ps1)、[`.github/workflows/ci.yml`](../.github/workflows/ci.yml)）。
> 本套件**不进默认 CI**；不解锁 `TTS_WRITE_*`；不提交密钥。

`tests/real/` 下用 `@pytest.mark.real` 标记的用例，对外部服务做**只读**探测：TikTok Shop 店铺信息 / 商品列表 / 订单列表、1688 商品查询、LLM 一次最小 `chat/completions`。缺凭据时 `skip`，reason 只列需要的环境变量名。

## 运行方式

在仓库根目录，先把 `.env.example` 复制为 `.env` 并填入真实值（`.env` 已 gitignore，勿提交）。

### Linux / macOS

```bash
cp -n .env.example .env   # 若尚无 .env
export PYTHONPATH=src
make test-real
# 等价：
python3 -m pytest -m real tests/real -v
```

### Windows PowerShell

```powershell
Copy-Item .env.example .env -ErrorAction SilentlyContinue
$env:PYTHONPATH = "src"
python -m pytest -m real tests/real -v
```

无 `make` 时直接用上面的 `pytest` 行即可。

### 与默认门的关系

| 命令 | 作用 |
|---|---|
| `pytest -m "not real"` / `scripts/ci.ps1` / GitHub CI | 离线确定性门；**排除**本套件与其它 `@pytest.mark.real` |
| `make test-real` / `pytest -m real tests/real` | 本机可选真 API 只读探测 |

`tests/real/test_suite_guards.py` **无** `real` 标记：静态检查源码不调用写接口，随默认 CI 跑。

## 所需环境变量（占位，勿把真值写进文档或 Git）

见仓库根 [`.env.example`](../.env.example)。本套件用到的名字：

### TikTok Shop（只读）

| 变量 | 说明 |
|---|---|
| `TIKTOK_APP_KEY` | 开放平台应用 key |
| `TIKTOK_APP_SECRET` | 应用 secret |
| `TIKTOK_ACCESS_TOKEN` | 访问令牌 |
| `TIKTOK_REFRESH_TOKEN` | 可选；过期时可刷新 |
| `TIKTOK_SHOP_CIPHER` | 可选；缺省时由授权店铺接口解析 |
| `TIKTOK_SHOP_REGION` | 可选；默认 `TH` |
| `TIKTOK_API_BASE` | 可选；默认 `https://open-api.tiktokglobalshop.com` |

覆盖：`get_authorized_shops` / `search_products` / `get_orders`。写路径仍返回 `TTS_WRITE_BLOCKED`。

### TikTok Ads（只读花费）

| 变量 | 说明 |
|---|---|
| `TIKTOK_ADS_ENABLED` | 须为 `1`（或 true/yes/on） |
| `TIKTOK_ADS_ACCESS_TOKEN` | Marketing API Access-Token。只放请求头 |
| `TIKTOK_ADS_ADVERTISER_ID` | 广告主 ID |
| `TIKTOK_ADS_API_BASE` | 可选；只能是 `https://business-api.tiktok.com/open_api/v1.3` |

覆盖：`pull_spend` 拉昨天一天。只 GET `advertiser/info` 与 `report/integrated/get`。不改预算。广告主币种不是 THB 时用例失败并说明，不换汇。

另有 `tests/real/test_tts_preflight_real.py`：对 `shopkeeper tts-preflight`（HANDOFF_PLAN P0-02）做真凭据只读探测，断言预检结论结构化、且输出不含密钥原文。

### 1688 货源（只读）

| 变量 | 说明 |
|---|---|
| `ALIBABA_1688_ENABLED` | 须为 `1`（或 true/yes/on） |
| `ALIBABA_1688_APP_KEY` | 应用 key |
| `ALIBABA_1688_APP_SECRET` | 应用 secret |
| `ALIBABA_1688_ACCESS_TOKEN` | 访问令牌 |
| `ALIBABA_1688_TEST_OFFER_ID` | 用于查询的 offerId（数字） |
| `ALIBABA_1688_TEST_OFFER_URL` | 可选；完整 detail 链接，优先于 offerId |
| `ALIBABA_1688_API_BASE` | 可选；默认开放平台网关 |

### LLM（一次最小调用）

| 变量 | 说明 |
|---|---|
| `SHOPKEEPER_LLM_BASE_URL` | OpenAI 兼容根；默认 `http://127.0.0.1:3425/v1` |
| `SHOPKEEPER_LLM_API_KEY` | 密钥；本机 magpie 可用占位 `local-gateway-no-auth` |
| `SHOPKEEPER_LLM_MODEL` | 模型 id；默认 `group/auto-deepseek-v4-1-flash` |

## 硬约束

1. **不解锁 TTS 真写**：本套件不设置 `TTS_WRITE_ENABLED=1`，不构造 `TikTokShopClient(allow_writes=True)`，不调用上架 / 发货 / 退款 / 下单 / 付款写接口。
2. **不提交密钥**：真实值只进本机 `.env`；文档与用例只写变量名。
3. **默认 CI 全绿**：`pytest -m "not real"` 不得依赖外网或本套件凭据。

产品与闸门口径仍以 [DESIGN.md](./DESIGN.md) / [SCHEMA.md](./SCHEMA.md) 为准；真写人工验收见 [phase5-real-listing-acceptance.md](./phase5-real-listing-acceptance.md)。
