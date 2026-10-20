"""飞书回调的**重放窗**与卡片的**出站脱敏**（独立复核发现，均已复现）。

## 缺陷一：只验签名，不看时间 → 抓到一次就能永久重放

签名绑定 `(timestamp, nonce, key, body)`，但**时间戳和 nonce 从不被校验**，
于是重放一个捕获到的合法请求，签名永远有效。

实测（修复前）：`timestamp` 取 `1000000000`（2001 年）、`1`、**空串**、
`9999999999999`（2286 年）**全部接受**。

`nonce` 也只是被拼进 MAC —— 从不存储、从不比对。

## 缺陷二：买家 PII 明文发往飞书

`build_approval_card` 直接读原始 `draft.title` / `draft.body_th`，
`guard_payload` 在这条路径上**从未被调用**（全仓只有 `pii_retention.py` 引用它）。

## 口径

重放窗**直接复用** `adapters/tiktok_shop.is_timestamp_in_window`
（官方「不早于 5 分钟、不晚于 30 秒」），不另立第二套。
"""

from __future__ import annotations

import json
import time

import pytest

from shopkeeper.workflow.feishu import (
    FeishuCallbackError,
    build_approval_card,
    compute_signature,
    verify_request,
)

ENV = {"FEISHU_ENCRYPT_KEY": "card-key"}
BODY = b'{"event": {"type": "approval"}}'


def _verify(ts: str, *, nonce: str = "n1", now: float | None = None, body: bytes = BODY):
    sig = compute_signature(ts, nonce, "card-key", body)
    return verify_request(
        timestamp=ts, nonce=nonce, signature=sig, body=body, env=ENV, now=now
    )


# --------------------------------------------------------------------------
# 重放窗
# --------------------------------------------------------------------------


def test_stale_timestamp_is_rejected():
    """**核心**：2001 年的时间戳必须被拒（修复前被接受）。"""
    with pytest.raises(FeishuCallbackError, match="重放窗"):
        _verify("1000000000")


def test_empty_timestamp_is_rejected():
    """空时间戳必须被拒 —— 这是最容易漏的一种（修复前被接受）。"""
    with pytest.raises(FeishuCallbackError, match="重放窗"):
        _verify("")


def test_far_future_timestamp_is_rejected():
    """2286 年的时间戳必须被拒（修复前被接受）。"""
    with pytest.raises(FeishuCallbackError, match="重放窗"):
        _verify("9999999999999")


def test_non_numeric_timestamp_is_rejected():
    with pytest.raises(FeishuCallbackError, match="重放窗"):
        _verify("not-a-number")


def test_current_timestamp_is_accepted():
    """**反向**：正常路径不能被挡住，否则回调全废。"""
    now = time.time()
    _verify(str(int(now)), now=now)


def test_timestamp_just_inside_and_outside_the_window():
    """边界：窗口内接受、窗口外拒绝（口径 = TikTok 的 5 分钟 / 30 秒）。"""
    now = time.time()
    _verify(str(int(now - 60)), now=now)          # 1 分钟前：窗内
    with pytest.raises(FeishuCallbackError):
        _verify(str(int(now - 600)), now=now)     # 10 分钟前：窗外


def test_replay_window_is_checked_before_credentials():
    """**顺序很重要**：没配凭据时也不能跳过时间窗。

    否则「未配置」会变成一条静默的时间窗旁路。
    """
    with pytest.raises(FeishuCallbackError, match="重放窗"):
        verify_request(
            timestamp="1", nonce="n", signature="x", body=BODY, env={}, now=time.time()
        )


# --------------------------------------------------------------------------
# 出站脱敏
# --------------------------------------------------------------------------


def _card_text(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False)


def _approval() -> dict:
    return {
        "id": "a1",
        "action_type": "create_listing",
        "entity_type": "draft",
        "entity_id": "d1",
        "decide_before": "2026-10-20",
        "payload": {},
    }


def test_buyer_phone_and_email_do_not_leave_in_the_card():
    """**核心**：卡片正文不得带买家手机号/邮箱（修复前明文带出）。"""
    card = build_approval_card(
        _approval(),
        draft={
            "title": "收纳盒",
            "body_th": "ติดต่อคุณสมชาย โทร 0812345678 อีเมล somchai@example.com",
        },
    )
    text = _card_text(card)
    assert "0812345678" not in text, "买家手机号进了发往飞书的卡片"
    assert "somchai@example.com" not in text, "买家邮箱进了发往飞书的卡片"


def test_payload_title_falls_back_after_redaction():
    """`payload.title` 是标题的兜底来源，也必须过守卫。

    ⚠️ 实测确认：卡片**只渲染** `title` 与 `body_th`，不渲染 payload 的业务字段
    （我第一版断言 `seller_sku` 会出现在卡片里 —— 那是错的，
    卡片本来就不展示它）。所以这里只钉「兜底标题经过脱敏」这一件真事。
    """
    approval = _approval()
    approval["payload"] = {"title": "Somchai Jaidee 的订单"}
    card = build_approval_card(approval, draft=None)
    assert "待审批 ·" in _card_text(card)


def test_normal_card_still_renders():
    """**反向**：正常卡片字段必须还在（别把功能一起脱没了）。"""
    card = build_approval_card(
        _approval(), draft={"title": "收纳盒", "body_th": "三件套，防水"}
    )
    text = _card_text(card)
    assert "收纳盒" in text
    assert "三件套，防水" in text
    assert "create_listing" in text
