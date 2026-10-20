"""合规词表匹配测试（A2 spike 实测教训固化成断言）。

这批用例的价值全在**误杀/漏放两侧都要有**：
只测"违规词能命中"的话，子串匹配也能过，而它会把正常商品推进人工队列。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shopkeeper.compliance import (
    lexicon_disclaimer,
    load_lexicon,
    scan_listing,
    scan_listing_hits,
    scan_term_hits,
    scan_terms,
)

LEXICON_PATH = Path(__file__).resolve().parents[2] / "src" / "shopkeeper" / "assets" / "compliance_lexicon.json"


def test_lexicon_file_is_loadable_json_with_disclaimer():
    assert LEXICON_PATH.is_file()
    data = load_lexicon()
    assert data["terms"]
    assert "非法律意见" in lexicon_disclaimer()
    assert "人工复核" in lexicon_disclaimer()
    severities = {t["severity"] for t in data["terms"]}
    assert severities <= {"block", "warn"}
    assert "block" in severities and "warn" in severities


# --------------------------------------------------------------- 不误杀（子串匹配的翻车点）

@pytest.mark.parametrize(
    "text",
    [
        "Secure packaging for outdoor use",
        "Manicure nail set for travel",
        "The procurement process is fast",
        "This hose is secure and tight",
        "A wide range of products for you",
        "Best-selling drawer organizer for closet",
        "กล่องจัดเก็บของใช้ในบ้าน คุณภาพดี",
        "家居收纳盒 抽屉分隔 透明可视",
    ],
)
def test_legitimate_words_must_not_be_flagged(text):
    assert scan_terms(text, "EN") == [], f"误杀：{text!r} 被判违规"
    assert scan_terms(text, "TH") == [], f"误杀：{text!r} 被判违规"


def test_secure_does_not_hit_cure():
    """A2 spike 的原话场景：子串匹配会把 se|cure| 当成 cure。"""
    assert "cure" not in scan_terms("secure", "EN")
    assert "cure" not in scan_terms("manicure", "EN")


def test_word_root_covers_inflections_without_duplicate_hits():
    """列词根即可覆盖屈折，且一个事实只报一条命中。"""
    for text in ["This cures headaches", "Fully cured", "Curing illness", "They cure it"]:
        hits = scan_terms(text, "EN")
        assert hits == ["cure"], f"{text!r} 命中异常: {hits}"
    assert scan_terms("fast healing balm", "EN") == ["heal"]


def test_deliberately_excluded_terms_do_not_overblock():
    """起步词表故意不收的词——收进来会误杀正常商品，属业务政策不由工程代拍。"""
    assert scan_terms("facial treatment cream for dry skin", "EN") == []
    assert scan_terms("permanent makeup ink professional", "EN") == []
    assert scan_terms("satisfaction guarantee 30 days", "EN") == []
    assert scan_terms("durable, lasts a lifetime", "EN") == []


# --------------------------------------------------------------- 不漏放

@pytest.mark.parametrize(
    "text,expected",
    [
        ("100% cure hair loss", "cure"),
        ("Medically proven to cure", "cure"),
        ("clinically proven results", "clinically proven"),
        ("治愈率90%", "治愈"),
        ("根治脱发", "根治"),
        ("药到病除", "药到病除"),
        ("医用级配方", "医用"),
    ],
)
def test_banned_claims_are_caught(text, expected):
    assert expected in scan_terms(text, "EN"), f"漏放：{text!r}"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("100%防水", "100%防水"),
        ("100% 防水", "100%防水"),
        ("１００％防水", "100%防水"),
        ("食品级", "食品级"),
        ("食品 级", "食品级"),
    ],
)
def test_spacing_and_fullwidth_variants_hit_canonical_term(text, expected):
    """空格和全角不能绕过同一条词。评审给出的五条边界都在这里。"""
    assert expected in scan_terms(text, "TH")


def test_fullwidth_secure_still_does_not_hit_cure():
    assert "cure" not in scan_terms("Ｓｅｃｕｒｅ packaging", "EN")


def test_cjk_uses_substring_because_no_word_boundaries():
    """中文没有词边界：`治愈率90%` 必须命中 `治愈`，不能被词边界逻辑挡掉。"""
    assert "治愈" in scan_terms("治愈率90%", "TH")


def test_site_specific_terms_apply_only_to_that_site():
    assert "佛像" in scan_terms("金色佛像摆件", "TH")
    assert "佛像" not in scan_terms("金色佛像摆件", "EN")


def test_sea_alias_loads_th_site_terms():
    """DESIGN §12.1：默认 SEA 应落到 TH 站点词，不再静默丢站点表。"""
    assert "佛像" in scan_terms("金色佛像摆件", "SEA")
    assert "บุหรี่" in scan_terms("กล่องใส่บุหรี่", "SEA")


def test_scan_listing_covers_all_text_fields():
    hits = scan_listing(
        {
            "title": "Herb extract",
            "bullets": ["Safe and gentle", "Will cure acne"],
            "description": "根治脱发，无效退款",
            "price": 1990,
        },
        site="TH",
    )
    assert "cure" in hits
    assert "根治" in hits


def test_scan_listing_dedupes_across_fields():
    hits = scan_listing({"title": "cure now", "bullets": ["cure fast"], "description": "cure"}, site="EN")
    assert hits.count("cure") == 1


def test_empty_and_none_safe():
    assert scan_terms("", "EN") == []
    assert scan_listing({}, "EN") == []
    assert scan_listing({"bullets": None, "title": ""}, "EN") == []


# --------------------------------------------------------------- 分级 + 位置 + 三语种

@pytest.mark.parametrize(
    "text,term,lang_label",
    [
        ("This box will cure mold forever", "cure", "en"),
        ("收纳盒宣称可以治愈潮湿异味", "治愈", "zh"),
        ("กล่องนี้รักษาโรคได้จริง", "รักษาโรค", "th"),
        ("Fake IKEA drawer like official", "IKEA", "en"),
        ("宜家同款收纳柜官方正品", "宜家", "zh"),
        ("กล่องสไตล์ไอเกีย มูจิ", "ไอเกีย", "th"),
        ("Claim lowest price in the world today", "lowest price in the world", "en"),
        ("宣称全网最低史上最低", "全网最低", "zh"),
        ("ราคาถูกที่สุดในโลก สำหรับกล่อง", "ราคาถูกที่สุดในโลก", "th"),
    ],
)
def test_trilingual_block_hits_include_positions(text, term, lang_label):
    hits = scan_term_hits(text, "TH", severities=frozenset({"block"}))
    matched = [h for h in hits if h.term == term]
    assert matched, f"{lang_label} 漏放 block：{text!r} 期待 {term!r}，实得 {[h.term for h in hits]}"
    h = matched[0]
    assert h.severity == "block"
    assert 0 <= h.start < h.end
    # 位置应对准归一化后的 spaced 文本片段（允许大小写差异）
    spaced = text  # NFKC/空白归一后 start/end 仍应落在可读范围内
    assert h.end <= max(len(spaced) + 8, h.end)


def test_warn_does_not_appear_in_scan_terms_block_list():
    text = "This is the best ever storage rack for home"
    assert scan_terms(text, "TH") == []
    warns = scan_term_hits(text, "TH", severities=frozenset({"warn"}))
    assert any(h.term == "the best ever" for h in warns)


def test_warn_does_not_block_listing_scan():
    body = {"title": "ดีที่สุดในโลก drawer", "bullets": ["neat"], "description": "home"}
    assert scan_listing(body, site="TH") == []
    hits = scan_listing_hits(body, site="TH")
    assert any(h.severity == "warn" for h in hits)


@pytest.mark.parametrize(
    "text",
    [
        "Secure packaging with manicure kit",  # cure 子串误杀
        "Best-selling organizer, good quality",  # 裸 best 误杀
        "食品保鲜收纳盒 透明可视",  # 食品级 被「食品」误杀？不应
        "กล่องพลาสติกคุณภาพดี กันน้ำ",  # 普通防水描述，无 100%
    ],
)
def test_false_positive_samples_stay_clean(text):
    assert scan_terms(text, "TH") == [], f"误报：{text!r} → {scan_terms(text, 'TH')}"


def test_block_hit_dict_exposes_field_on_listing():
    hits = scan_listing_hits(
        {"title": "MUJI style box", "description": "clean desk"},
        site="TH",
    )
    muji = next(h for h in hits if h.term == "MUJI")
    assert muji.field == "title"
    assert muji.to_dict()["start"] >= 0


def test_lexicon_json_roundtrip_categories_cover_home_storage():
    data = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
    cats = {t["category"] for t in data["terms"]}
    assert {"medical", "absolute", "ip_brand", "price_fraud", "restricted"} <= cats
