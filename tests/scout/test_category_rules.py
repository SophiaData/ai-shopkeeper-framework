"""P2-08 选品报告增强：家居收纳类目规则与竞品价带。

## 缺口

类目判断此前只存在于**散文文档**（手册 §2.1）与提示词里，**代码里没有任何类目概念**：
`research_digest` 对所有行一视同仁，既不提示「这个品不适合短视频」，也不标价带偏离。

## 价带数字的性质（用例重点）

价带是本仓库整理的**参考区间**，**不是抓取的竞品数据**（当前无法访问竞品价格接口）。
所以用例钉住的是「**不确定时要说出来**」：类目认不出、售价解析不出 → `unknown`，
而不是编一个结论。
"""

from __future__ import annotations

import pytest

from shopkeeper.scout.category_rules import (
    REFERENCE_PRICE_BANDS,
    assess_category,
    assess_price,
    assess_video_fit,
    match_category,
    missing_pain_themes,
    render_category_section,
)


# --------------------------------------------------------------------------
# 类目识别
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("折叠收纳盒 PP 材质", "收纳盒"),
        ("厨房置物架 免打孔", "置物架"),
        ("防滑衣架 10 个装", "衣架"),
        ("真空压缩袋 收纳", "真空袋"),
        ("浴室收纳 三角架", "浴室收纳"),
    ],
)
def test_match_category(text, expected):
    assert match_category(text) == expected


def test_unrecognized_category_is_empty_not_guessed():
    """认不出就返回空串 —— **不猜**。"""
    assert match_category("某种说不清的品") == ""
    assert match_category("") == ""


def test_longer_keyword_wins():
    """`收纳盒` 不该被 `收纳` 抢先匹配（否则类目会错）。"""
    assert match_category("折叠收纳盒") == "收纳盒"


# --------------------------------------------------------------------------
# 价带
# --------------------------------------------------------------------------


def test_price_in_band():
    status, note = assess_price(199, "收纳盒")
    assert status == "in_band"
    assert "99" in note and "399" in note


def test_price_below_band_warns_about_cost():
    status, note = assess_price(9, "收纳盒")
    assert status == "below"
    assert "成本" in note or "佣金" in note


def test_price_above_band_needs_reason():
    status, note = assess_price(9999, "收纳盒")
    assert status == "above"
    assert "差异化" in note


def test_unknown_category_price_is_unknown_not_in_band():
    """类目未知时**不能说「在价带内」** —— 那需要先有价带。"""
    status, note = assess_price(199, "")
    assert status == "unknown"
    assert "未做偏离判断" in note


@pytest.mark.parametrize("raw", [None, "", "abc", True, False, 0, -5, float("nan"), float("inf")])
def test_unparseable_price_is_unknown(raw):
    """bool / 非正数 / 非有限数都不是售价。float(True)==1 会误判成低于价带。"""
    status, note = assess_price(raw, "收纳盒")
    assert status == "unknown"
    assert "未做偏离判断" in note


def test_bands_are_sane():
    """价带必须低 < 高，且为正 —— 防手滑写反。"""
    for name, (low, high) in REFERENCE_PRICE_BANDS.items():
        assert 0 < low < high, f"{name} 的价带不合法：{low}-{high}"


# --------------------------------------------------------------------------
# 短视频适配
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["折叠收纳盒", "免打孔置物架", "真空压缩袋", "可伸缩杆"])
def test_video_friendly_signals(text):
    fit, note = assess_video_fit(text)
    assert fit is True
    assert "可拍" in note


@pytest.mark.parametrize("text", ["落地大衣柜", "整柜定制", "床垫 大件"])
def test_bulky_signals_are_unfriendly(text):
    fit, note = assess_video_fit(text)
    assert fit is False
    assert "大件" in note


def test_no_signal_is_none_not_false():
    """没看到信号 ≠ 不适合。返回 None 让人来判断。"""
    fit, note = assess_video_fit("普通商品")
    assert fit is None
    assert "需人工判断" in note


def test_bulky_wins_over_friendly_signal():
    """既是「大件」又含「折叠」，应按大件处理（更保守）。"""
    fit, _ = assess_video_fit("落地大件 可折叠")
    assert fit is False


# --------------------------------------------------------------------------
# 痛点话术覆盖
# --------------------------------------------------------------------------


def test_all_themes_missing_for_empty_text():
    assert set(missing_pain_themes("")) == {"潮湿", "空间小", "不能打孔"}


def test_themes_covered_by_synonyms():
    """提到近义词即算覆盖。"""
    assert missing_pain_themes("防潮 出租公寓 免打孔") == []


def test_partial_coverage():
    assert missing_pain_themes("防潮 防霉") == ["空间小", "不能打孔"]


# --------------------------------------------------------------------------
# 综合
# --------------------------------------------------------------------------


def test_assess_returns_all_fields():
    a = assess_category(text="折叠收纳盒 免打孔 省空间 防潮", price_thb=199)
    d = a.as_dict()
    assert set(d) == {
        "category",
        "price_band",
        "price_status",
        "video_friendly",
        "video_note",
        "missing_pain_themes",
        "bulky",
        "notes",
    }


def test_assess_explicit_category_wins():
    a = assess_category(text="随便", price_thb=199, category="收纳盒")
    assert a.category == "收纳盒"
    assert a.price_status == "in_band"


def test_assess_unknown_category_says_so():
    a = assess_category(text="说不清的品")
    assert a.category == ""
    assert any("未能识别类目" in n for n in a.notes)


def test_assess_notes_mention_missing_themes():
    a = assess_category(text="落地大衣柜", price_thb=999)
    assert any("话术未覆盖" in n for n in a.notes)


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------


def test_render_section_states_band_is_not_scraped():
    """报告必须写明价带**不是抓取的竞品数据** —— 否则读者会当成实测。"""
    lines = render_category_section([{"name": "A", "category_assessment": assess_category(text="收纳盒", price_thb=199).as_dict()}])
    text = "\n".join(lines)
    assert "非抓取的竞品实测数据" in text


def test_render_section_empty_rows():
    text = "\n".join(render_category_section([]))
    assert "本节为空" in text


def test_render_section_escapes_pipe():
    """品名里的 `|` 会破坏表格 —— 必须转义。"""
    lines = render_category_section([{"name": "A|B", "category_assessment": {}}])
    row = [x for x in lines if x.startswith("| A")]
    assert row and "A/B" in row[0]


def test_render_section_handles_missing_assessment():
    lines = render_category_section([{"name": "A"}])
    assert any("—" in x for x in lines)


# --------------------------------------------------------------------------
# 端到端：真实报告里出现这一节
# --------------------------------------------------------------------------


def test_research_digest_includes_category_section():
    from shopkeeper.workflow.research_digest import render

    rows = [
        {
            "name": "折叠收纳盒",
            "pain": "潮湿 空间小 不能打孔",
            "scene": "浴室",
            "diff": "可折叠",
            "price_band": "199",
            "supply_status": "可供货",
        }
    ]
    md = render(rows, "test.csv")
    assert "## 类目适配与价带参考" in md
    assert "收纳盒" in md
    assert "99–399" in md


def test_research_digest_handles_unparsable_band():
    """价带文本是 `面议` 这类时不该崩，只是不做价带判断。"""
    from shopkeeper.workflow.research_digest import render

    rows = [
        {
            "name": "收纳盒",
            "pain": "潮湿 空间小 不能打孔",
            "scene": "浴室",
            "diff": "可折叠",
            "price_band": "面议",
            "supply_status": "可供货",
        }
    ]
    md = render(rows, "test.csv")
    assert "unknown" in md


def test_first_price_strips_thousands_separator():
    """`฿1,299` 必须是 **1299**，不是 1。

    朴素正则会只匹配到 `1`，于是 1,299 THB 的商品被判成「低于价带 99–399，
    请确认能否覆盖成本」—— **结论完全反了**，会让人给高价品做无意义的成本核查。
    """
    from shopkeeper.workflow.research_digest import _first_price

    assert _first_price("฿1,299") == 1299.0
    assert _first_price("1,299") == 1299.0
    assert _first_price("1,299.50") == 1299.5
    assert _first_price("12,345.67") == 12345.67
    assert _first_price("1 299") == 1299.0


def test_thousands_separator_gives_correct_verdict():
    """端到端：千分位价格必须被判为 above，而不是 below。"""
    from shopkeeper.scout.category_rules import assess_price
    from shopkeeper.workflow.research_digest import _first_price

    status, note = assess_price(_first_price("฿1,299"), "收纳盒")
    assert status == "above", "1299 高于价带 99–399，必须判 above"
    assert "高于" in note


def test_research_digest_renders_correct_verdict_for_thousands():
    """端到端：报告里不能出现「1299 的商品低于价带」这种反向结论。"""
    from shopkeeper.workflow.research_digest import render

    rows = [
        {
            "name": "折叠收纳盒",
            "pain": "潮湿 空间小 不能打孔",
            "scene": "浴室",
            "diff": "可折叠",
            "price_band": "฿1,299",
            "supply_status": "可供货",
        }
    ]
    md = render(rows, "test.csv")
    assert "| above |" in md, "千分位价格应判 above"
    assert "| below |" not in md


def test_research_digest_parses_range_band():
    """`99-199` 这种区间文本应取第一个数字做判断。"""
    from shopkeeper.workflow.research_digest import _first_price

    assert _first_price("99-199") == 99.0
    assert _first_price("฿149") == 149.0
    assert _first_price("面议") is None
    assert _first_price(None) is None
