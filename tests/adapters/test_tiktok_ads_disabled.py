"""P3-04 TikTok Ads 只读接入：**保持 disabled** 的结构保证。

## 背景（2026-10-16 实测）

P3-04 要求「`adapters/tiktok_ads.py` 只读花费」。实测探测后发现**无法取得契约**：

| 探测 | 结果 |
|---|---|
| `business-api.tiktok.com` | 301 → `/portal`（200，36KB） |
| `ads.tiktok.com/marketing_api/docs` | 301 → `/portal/docs/about-the-guide/v1.3`（200） |
| 页面 `__NEXT_DATA__` | **仅** `seoTitle`/`seoDescription`（544 字符） |
| 契约关键词（`advertiser_id`/`report`/`endpoint`/`metrics`） | **全部 0 命中** |
| `/portal/api/docs/...`、`/_next/data/...` | 404 |

即 Next.js SPA，正文需**客户端渲染 + 登录态**。按 AGENTS 规则 4 **不臆造端点**。

所以本文件钉住的不是「接好了」，而是「**没接好时不会偷偷外呼**」——
这类「我什么都没做」的保证，最容易在后续改动里被悄悄破坏。
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from shopkeeper.adapters.tiktok_ads import (
    AdsSpendSource,
    MockTikTokAds,
    TikTokAdsReadClient,
    build_ads_source,
    refuse_ads_write,
)

MODULE = Path(__file__).resolve().parents[2] / "src" / "shopkeeper" / "adapters" / "tiktok_ads.py"


@pytest.fixture(scope="module")
def source() -> str:
    return MODULE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 结构保证：模块里根本没有网络路径
# --------------------------------------------------------------------------


def test_module_imports_no_network_library(source: str):
    """只允许 httpx。requests/urllib 不进这个模块。"""
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    assert "httpx" in imported
    for forbidden in ("requests", "urllib3", "aiohttp", "socket"):
        assert forbidden not in imported, f"tiktok_ads 引入了网络库 {forbidden}"
    # urllib 只允许 urlsplit 校验基址，不允许 urllib.request 发请求。
    assert "urllib.request" not in source
    assert "from urllib.parse import urlsplit" in source


def test_module_has_no_write_http_calls(source: str):
    """唯一的 HTTP 调用是 get。post/put/patch/delete 出现即失败。"""
    tree = ast.parse(source)
    verbs = {"post", "put", "patch", "delete"}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in verbs:
                found.append(node.func.attr)
    assert found == []
    for banned in (
        "/campaign/update/",
        "/campaign/create/",
        "/adgroup/update/",
        "/ad/update/",
        "/advertiser/update/",
        "budget/update",
    ):
        assert banned not in source
    assert "follow_redirects=False" in source


def test_read_client_has_no_client_attribute():
    """占位客户端不该持有 base_url / client 之类可外呼的属性。"""
    client = TikTokAdsReadClient()
    for attr in ("_client", "client", "base_url", "_base_url", "session", "transport"):
        assert not hasattr(client, attr), f"占位客户端不该有 {attr}"


def test_read_client_declares_disabled_name():
    """名字里要能看出「disabled」，避免日志里被误认成可用通道。"""
    assert "disabled" in TikTokAdsReadClient.name


# --------------------------------------------------------------------------
# 行为：调用必失败，且错误信息说明原因与出路
# --------------------------------------------------------------------------


def test_pull_spend_fails_with_explicit_code():
    result = TikTokAdsReadClient().pull_spend("s1", "2026-10-01", "2026-10-07")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "ADS_LIVE_DISABLED"


def test_error_message_says_why_and_what_next():
    """报错要说清「为什么不能」与「怎么才能」—— 否则接手的人只会反复试。"""
    result = TikTokAdsReadClient().pull_spend("s1", "2026-10-01", "2026-10-07")
    message = result.error.message
    assert "无法核对" in message or "未接通" in message
    assert "owner" in message, "应指明出路（需要 owner 提供凭据与文档）"


def test_refuse_error_message_explains_policy():
    result = TikTokAdsReadClient().pull_spend("s1", "2026-10-01", "2026-10-07")
    assert "不改预算" in result.error.message


# --------------------------------------------------------------------------
# build_ads_source：**永远**返回 mock
# --------------------------------------------------------------------------


def test_build_returns_mock_by_default(monkeypatch):
    monkeypatch.delenv("TIKTOK_ADS_ENABLED", raising=False)
    assert isinstance(build_ads_source(), MockTikTokAds)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_enabled_without_credentials_does_not_call_or_mock(monkeypatch, value):
    """开关打开但没凭据：不回退 mock（假花费会进账），也不发请求。"""
    monkeypatch.setenv("TIKTOK_ADS_ENABLED", value)
    monkeypatch.delenv("TIKTOK_ADS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TIKTOK_ADS_ADVERTISER_ID", raising=False)

    def explode(request):  # noqa: ARG001
        raise AssertionError("缺凭据不应该发 HTTP")

    source = build_ads_source(transport=httpx.MockTransport(explode))
    assert isinstance(source, TikTokAdsReadClient)
    assert source.configured is False
    result = source.pull_spend("s1", "2026-10-01", "2026-10-01")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "ADS_CREDENTIALS_MISSING"


def test_explicit_env_off_stays_mock_even_with_token():
    """传入的 env 关着开关时，不读进程里的令牌。"""
    source = build_ads_source(
        {
            "TIKTOK_ADS_ENABLED": "0",
            "TIKTOK_ADS_ACCESS_TOKEN": "should-not-be-used",
            "TIKTOK_ADS_ADVERTISER_ID": "7000000000000000001",
        }
    )
    assert isinstance(source, MockTikTokAds)


# --------------------------------------------------------------------------
# 写操作永久拒绝
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["update_budget", "create_campaign", "set_bid", ""])
def test_write_actions_refused(action):
    result = refuse_ads_write(action)
    assert result.ok is False
    assert result.error.code == "ADS_WRITE_REFUSED"


def test_write_refusal_is_unconditional():
    """**没登记过的动作也要拒绝** —— 白名单反了会把「没登记」变成「可以写」。"""
    for action in ("update_budget", "某个从没见过的动作", "", "DELETE_EVERYTHING"):
        result = refuse_ads_write(action)
        assert result.ok is False, f"{action!r} 竟然没被拒绝"
        assert result.error.code == "ADS_WRITE_REFUSED"


def test_registered_actions_are_a_real_list():
    """`REFUSED_WRITE_ACTIONS` 不能是没人引用的死代码。"""
    from shopkeeper.adapters import tiktok_ads

    assert tiktok_ads.REFUSED_WRITE_ACTIONS, "动作表不能为空"
    # 表里的每个动作都必须真的被拒绝
    for action in tiktok_ads.REFUSED_WRITE_ACTIONS:
        assert refuse_ads_write(action).ok is False


def test_unknown_action_says_so_in_message():
    """未登记动作要在消息里说明 —— 便于排查调用方写了什么。"""
    message = refuse_ads_write("某动作").error.message
    assert "不在已登记动作表内" in message


def test_write_refusal_mentions_no_side_effects():
    message = refuse_ads_write("update_budget").error.message
    assert "不改预算" in message


def test_source_interface_has_no_write_methods():
    """`AdsSpendSource` **只有** `pull_spend` —— 结构上就没有改预算的口子。"""
    methods = {name for name in dir(AdsSpendSource) if not name.startswith("_")}
    assert "pull_spend" in methods
    for forbidden in ("update_budget", "create_campaign", "set_bid", "delete_campaign"):
        assert forbidden not in methods, f"接口暴露了写方法 {forbidden}"


def test_module_defines_no_write_functions(source: str):
    """模块里不该有除 `refuse_ads_write` 外的写函数。"""
    tree = ast.parse(source)
    functions = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    suspicious = [f for f in functions if any(k in f for k in ("budget", "campaign", "bid"))]
    assert suspicious == [], f"出现了可疑的写函数：{suspicious}"


# --------------------------------------------------------------------------
# mock 仍可用（回归：保持 disabled 不该弄坏 mock）
# --------------------------------------------------------------------------


def test_mock_still_returns_rows():
    rows = MockTikTokAds().pull_spend("s1", "2026-10-01", "2026-10-03")
    assert rows.ok is True
    assert len(rows.data) == 3
    assert rows.data[0]["currency"] == "THB"
    assert rows.data[0]["channel"] == "tiktok_ads"


def test_mock_row_shape_matches_documented_contract():
    """mock 的字段就是「接真实源时要产出什么」的契约 —— 钉住它。"""
    row = MockTikTokAds().pull_spend("s1", "2026-10-01", "2026-10-01").data[0]
    for key in (
        "shop_id",
        "sku",
        "product_id",
        "spend_date",
        "channel",
        "amount_minor",
        "currency",
    ):
        assert key in row, f"mock 行缺少 `{key}`，与文档说的契约不符"
    assert isinstance(row["amount_minor"], int), "金额必须是**分**（int）"
