"""多词拉丁合规词的连字符形态必须命中。

`re.escape("absolutely waterproof")` 只认**恰好一个空格**，于是英文里
正常的连字符拼写（`water-proof` / `food-grade`）**静默漏放**。

这不是刻意规避 —— 连字符是标准英文拼写，商品文案里很常见。
"""

from __future__ import annotations

import pytest

from shopkeeper.compliance import scan_listing_hits


@pytest.mark.parametrize(
    "text,expected",
    [
        ("absolutely waterproof", "absolutely waterproof"),
        ("absolutely water-proof", "absolutely waterproof"),
        ("absolutely  waterproof", "absolutely waterproof"),
        ("absolutely\nwaterproof", "absolutely waterproof"),
        ("ABSOLUTELY WATERPROOF", "absolutely waterproof"),
        ("food grade", "food grade"),
        ("food-grade", "food grade"),
        ("FOOD-GRADE", "food grade"),
    ],
)
def test_hyphenated_latin_terms_are_caught(text, expected):
    """词间与词内的连字符都要能吃住。"""
    terms = [h.term for h in scan_listing_hits({"title": text})]
    assert expected in terms, f"{text!r} 未命中 {expected!r}，实际 {terms}"


@pytest.mark.parametrize(
    "text",
    [
        "waterproof",           # 单独一个词不是合规词
        "absolutely waterproofxxx",
        "waterproofer",
        "normal marketing copy",
    ],
)
def test_no_false_positive(text):
    """放宽匹配不能引入误报 —— 过度拦截会挡住正常文案。"""
    assert scan_listing_hits({"title": text}) == []
