"""已批准的价格必须进入上架摘要（独立复核发现的 P1-4）。

## 缺陷

`_listing_payload` 的摘要原先只覆盖 title/body/input，**不含价格**。后果：

- 人工批准了偏离价（例如 21000）
- 之后草稿 payload 里的 `pricing` 被改动
- `payload_digest` **完全不变**

于是「**批准了 A 价、实际按 B 价上架**」这件事**无法被发现**。

## 为什么不往平台载荷里加价格字段

查过两侧实现：`TikTokShopClient.create_listing` 只发 `draft_id` + `payload_digest`，
平台侧价格由草稿本身决定。**加一个平台不认的 `price` 字段属于臆造外部契约**（§0）。
所以本改动**不新增外呼字段**，只把已批准价格纳入**摘要**，
让不一致成为**可检出**的事实。

**⚠️ 仍需 owner 决策**：平台是否支持在 create_listing 时带上价格，
需要 TikTok Shop 文档 + 真实凭据才能确定。在确认之前，
`pricing_override` 审批链的作用是**留痕与可审计**，不是直接定价。
"""

from __future__ import annotations

from shopkeeper.workflow.gate import _listing_payload


def _draft(price, source="approved_override"):
    return {
        "id": "d1",
        "title": "收纳盒",
        "body_th": "B",
        "body_en": "E",
        "payload": {
            "title_en": "Box",
            "input": {},
            "pricing": {"suggested_price_thb": price, "price_source": source},
        },
    }


def test_price_change_changes_digest():
    """**核心**：价格变了，摘要必须变 —— 否则差异不可检出。"""
    low = _listing_payload(_draft(21000))
    high = _listing_payload(_draft(99900))
    assert low["payload_digest"] != high["payload_digest"], (
        "批准的 21000 与 99900 摘要相同 → 「批准 A 价却按 B 价上架」无法被发现"
    )


def test_missing_price_differs_from_present_price():
    """批准时有价、上架时没价，同样必须改变摘要。"""
    with_price = _listing_payload(_draft(21000))
    no_price = _listing_payload(
        {"id": "d1", "title": "T", "body_th": "", "body_en": "", "payload": {"input": {}}}
    )
    assert with_price["payload_digest"] != no_price["payload_digest"]


def test_approved_price_is_carried_for_audit():
    """价格要作为**审计伴随字段**带出来（不参与平台调用）。"""
    body = _listing_payload(_draft(21000))
    assert body["approved_price_thb"] == 21000
    assert body["price_source"] == "approved_override"


def test_same_input_same_digest():
    """**反向**：内容与价格都没变时摘要必须稳定（否则每次上架都被当成「变了」）。"""
    assert (
        _listing_payload(_draft(21000))["payload_digest"]
        == _listing_payload(_draft(21000))["payload_digest"]
    )


def test_platform_payload_has_no_invented_price_field():
    """**不臆造外部契约**：不新增平台不认的字段。

    `TikTokShopClient.create_listing` 只读 `draft_id` / `payload_digest`，
    所以载荷里不该出现凭空造的 `price` / `price_thb` 之类键。
    """
    body = _listing_payload(_draft(21000))
    for invented in ("price", "price_thb", "price_cents", "retail_price"):
        assert invented not in body, f"不该出现臆造的字段 {invented}"
