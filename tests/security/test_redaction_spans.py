"""片段级脱敏（span redaction）的回归测试。

背景：Standards 轴 Review 抓到 §4.4 的 PII 守卫没接在
`products.raw_snapshot` / `listing_versions.payload` 上。接线过程中我先后踩到
两个相反的坑，两边都必须有测试钉住，别再翻烧饼：

① 对文案字段完全跳过内容嗅探 → 嵌在正文里的手机号明文入库（漏 PII）；
② 一律整值替换 → 商品型号 "Model 1234567890123456 fan" 被打成 [REDACTED]
   （损坏交付物，比漏脱敏更难发现）。

现行规则（两档）：
  营销正文字段 → 只替掉命中的那一段，其余文案保留；
  其它字符串字段 → 整值打掉（C3 的保守默认，另有测试钉着）。
卡号要"分组书写 + 过 Luhn"双条件才算，因为正文里的连续长数字绝大多数是
型号 / SKU / 平台订单号，与卡号同形。
"""

from __future__ import annotations

from shopkeeper.security.redaction import (
    REDACTED,
    _luhn_ok,
    guard_payload,
    redact_spans,
)


def _clean(payload):
    return guard_payload(payload, row_key="t:1", sniff=True)


# ------------------------------------------------ 方向一：漏 PII（曾经真实发生过）


def test_phone_embedded_in_copy_is_scrubbed_not_skipped():
    cleaned, rep = _clean({"versions": [{"lang": "EN", "description": "Call 13812345678 for wholesale"}]})
    desc = cleaned["versions"][0]["description"]
    assert "13812345678" not in desc, "§4.4 没给文案字段开豁免，嵌在正文里的手机号必须清掉"
    assert desc == "Call [REDACTED] for wholesale", f"应只替掉那一段: {desc}"
    assert "$.versions[0].description" in rep.span_redacted


def test_grouped_card_number_in_copy_is_scrubbed():
    cleaned, _ = _clean({"bullets": ["pay to 4111 1111 1111 1111 now"]})
    assert cleaned["bullets"][0] == "pay to [REDACTED] now"


def test_card_number_in_non_copy_field_is_scrubbed():
    """tracking_no 不在文案清单里，但真空号不能因为它叫 tracking 就放过。"""
    cleaned, _ = _clean({"tracking_no": "4111 1111 1111 1111"})
    assert "4111 1111 1111 1111" not in str(cleaned["tracking_no"])


def test_email_in_copy_is_scrubbed():
    cleaned, _ = _clean({"description": "mail sales@example.com for details"})
    assert "sales@example.com" not in cleaned["description"]


# ------------------------------------------- 方向二：损坏业务数据（同样真实发生过）


def test_contiguous_model_number_in_copy_survives():
    """16 位**连续**数字在正文里是型号/SKU 形态，不能当卡号打掉。"""
    cleaned, _ = _clean({"title": "Model 1234567890123456 fan"})
    assert "1234567890123456" in cleaned["title"], f"型号被误杀了: {cleaned['title']}"


def test_letter_prefixed_model_number_survives():
    cleaned, _ = _clean({"title": "Thermos 500ml model AB1234567890123"})
    assert "AB1234567890123" in cleaned["title"], f"型号被咬掉半截: {cleaned['title']}"


def test_platform_order_id_survives():
    """平台订单号 15~19 位纯数字，与卡号同形，必须原样保留。"""
    cleaned, _ = _clean({"order_id": "2026101012345678901"})
    assert cleaned["order_id"] == "2026101012345678901"


def test_spec_strings_survive():
    cleaned, _ = _clean({"bullets": ["57x30 55mm thermal paper", "尺寸 500ml 功率 22.5W"]})
    assert cleaned["bullets"][0] == "57x30 55mm thermal paper"
    assert cleaned["bullets"][1] == "尺寸 500ml 功率 22.5W"


def test_list_item_keeps_surrounding_text():
    """文案列表元素也必须片段替换，不能整条吞掉。"""
    cleaned, rep = _clean({"bullets": ["call 13800138000 to order"]})
    assert cleaned["bullets"][0] == "call [REDACTED] to order", "整条卖点被吞成 [REDACTED] 等于损坏交付物"
    assert rep.span_redacted


# ------------------------------------------------------- 字段名规则仍然优先


def test_pii_named_field_is_fully_redacted():
    cleaned, rep = _clean({"phone": "13812345678", "card_no": "6222021234567890123"})
    assert cleaned["phone"] == REDACTED
    assert cleaned["card_no"] == REDACTED
    assert set(rep.redacted) == {"$.phone", "$.card_no"}


def test_non_copy_free_text_field_is_fully_redacted():
    """note 一类字段没有"必须原样交付"的要求 → 整值打掉，不留上下文碎片。"""
    cleaned, rep = _clean({"note": "仓库 13812345678"})
    assert cleaned["note"] == REDACTED
    assert "仓库" not in str(cleaned["note"])


def test_plain_business_field_untouched():
    cleaned, rep = _clean({"normal_field": "keep me", "lang": "EN"})
    assert cleaned["normal_field"] == "keep me"
    assert rep.touched == 0


# ----------------------------------------------------------- Luhn 判定本身


def test_luhn_accepts_real_card_shape_and_rejects_random():
    assert _luhn_ok("4111111111111111") is True
    assert _luhn_ok("1234567890123456") is False


def test_redact_spans_is_idempotent():
    """重复守卫必须幂等，否则报告计数会虚高、二次清洗还会继续改数据。"""
    once, hit1 = redact_spans("Call 13812345678 now")
    twice, hit2 = redact_spans(once)
    assert once == "Call [REDACTED] now"
    assert twice == once
    assert hit2 is None
