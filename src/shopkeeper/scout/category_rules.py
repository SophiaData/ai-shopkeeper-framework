"""家居收纳类目的选品规则与参考价带（HANDOFF_PLAN P2-08）。

选品报告（`scout/pipeline.py`）、编排器选品步、以及 CSV 短名单共用这里：

1. 短视频适配：小体积、能拍前后对比的更适合（手册 §2.1）
2. 话术覆盖：潮湿 / 空间小 / 租房不能打孔
3. 参考价带：对照泰国站零售价，判断落在区间内、偏低或偏高

价带是本仓库整理的参考区间，不是抓取的竞品成交价。认不出类目、没有零售价、
或售价不是正数时返回 ``unknown``，不把 1688 人民币成本换算成泰铢再下结论。
偏离只作提醒，不阻断流程。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: 价带性质。报告、提示词、手册共用这一句，避免三处各写各的。
BAND_KIND = "reference_not_scraped"
BAND_DISCLAIMER = (
    "价带为本仓库整理的参考区间，非抓取的竞品实测数据；偏离只作提醒，不阻断流程。"
)

#: 没给泰国站零售价时的说明。1688 成本是人民币分，对不上泰铢价带。
NO_RETAIL_PRICE_NOTE = (
    "未提供泰国站零售价（THB）。1688 的 price_cents 是人民币成本（分），"
    "不能拿来对照泰铢参考价带，故不做偏离判断。"
)

#: 参考价带（THB）。**来源：手册 §2.1 的定性判断 + 常见零售区间整理，非抓取数据。**
#: `(低, 高)`；`None` 表示该类目暂无参考区间。
REFERENCE_PRICE_BANDS: dict[str, tuple[int, int]] = {
    "收纳盒": (99, 399),
    "置物架": (149, 499),
    "衣架": (59, 259),
    "真空袋": (99, 349),
    "挂钩": (39, 199),
    "浴室收纳": (129, 459),
    "厨房收纳": (99, 399),
    "桌面收纳": (79, 299),
}

#: 类目别名：同一类目的常见写法。**匹配用别名，价带仍查主名。**
CATEGORY_ALIASES: dict[str, str] = {
    "真空压缩袋": "真空袋",
    "压缩袋": "真空袋",
    "收纳箱": "收纳盒",
    "整理箱": "收纳盒",
    "毛巾架": "浴室收纳",
    "置物车": "置物架",
    "挂钩贴": "挂钩",
}

#: 适合短视频的场景信号（手册 §2.1：小体积、能拍前后对比）。
VIDEO_FRIENDLY_SIGNALS = (
    "折叠",
    "可折叠",
    "压缩",
    "真空",
    "免打孔",
    "前后对比",
    "堆叠",
    "伸缩",
)

#: 泰国站要覆盖的痛点话术（手册 §2.1 原文三条）。
REQUIRED_PAIN_THEMES: dict[str, tuple[str, ...]] = {
    "潮湿": ("潮", "霉", "湿", "防水", "防潮"),
    "空间小": ("空间", "小户型", "出租", "公寓", "省地", "紧凑"),
    "不能打孔": ("打孔", "免钉", "无痕", "粘贴", "免安装", "免打孔"),
}

#: 体积信号：太大不利于短视频与跨境运费。
BULKY_SIGNALS = ("大件", "落地", "整柜", "衣柜整体", "床垫", "沙发")


@dataclass
class CategoryAssessment:
    """一次类目适配判断的结果。"""

    category: str = ""
    price_band: tuple[int, int] | None = None
    price_status: str = "unknown"  # in_band / below / above / unknown
    video_friendly: bool | None = None
    video_note: str = ""
    missing_pain_themes: list[str] = field(default_factory=list)
    bulky: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "price_band": list(self.price_band) if self.price_band else None,
            "price_status": self.price_status,
            "video_friendly": self.video_friendly,
            "video_note": self.video_note,
            "missing_pain_themes": list(self.missing_pain_themes),
            "bulky": self.bulky,
            "notes": list(self.notes),
        }


def match_category(text: str) -> str:
    """从文本里认出类目。认不出返回空串 —— **不猜**。"""
    blob = str(text or "")
    # 别名优先且更长者优先 —— 避免「收纳盒」被「收纳」抢先、「真空袋」漏掉「真空压缩袋」。
    candidates = list(CATEGORY_ALIASES.items()) + [(k, k) for k in REFERENCE_PRICE_BANDS]
    for alias, canonical in sorted(candidates, key=lambda kv: len(kv[0]), reverse=True):
        if alias in blob:
            return canonical
    return ""


def assess_price(price_thb: Any, category: str) -> tuple[str, str]:
    """判断售价相对参考价带的位置。返回 `(status, note)`。

    类目未知或无参考区间 → `unknown` 并**明说未核对**，不假装有结论。
    """
    band = REFERENCE_PRICE_BANDS.get(category)
    if band is None:
        return "unknown", "该类目暂无参考价带，未做偏离判断。"
    # bool 是 int 的子类，float(True) == 1.0，会把「没给售价」判成低于价带。
    if isinstance(price_thb, bool):
        return "unknown", "售价无法解析，未做偏离判断。"
    try:
        price = float(price_thb)
    except (TypeError, ValueError):
        return "unknown", "售价无法解析，未做偏离判断。"
    if not math.isfinite(price) or price <= 0:
        return "unknown", "售价不是正数，未做偏离判断。"
    low, high = band
    if price < low:
        return "below", f"售价 {price:g} 低于参考价带 {low}–{high} THB，先确认能否覆盖成本与佣金。"
    if price > high:
        return "above", f"售价 {price:g} 高于参考价带 {low}–{high} THB，需差异化理由支撑。"
    return "in_band", f"售价 {price:g} 落在参考价带 {low}–{high} THB 内。"


def assess_video_fit(text: str) -> tuple[bool | None, str]:
    """判断是否适合短视频。返回 `(适合?, 说明)`。

    没有任何信号时返回 `None` —— 「没看到信号」不等于「不适合」。
    """
    blob = str(text or "")
    bulky_hits = [k for k in BULKY_SIGNALS if k in blob]
    if bulky_hits:
        return False, f"命中大件信号（{'、'.join(bulky_hits)}）：体积大不利于短视频展示，跨境运费也高。"
    hits = [k for k in VIDEO_FRIENDLY_SIGNALS if k in blob]
    if hits:
        return True, f"命中可拍信号（{'、'.join(hits)}）：能拍前后对比或演示，适合短视频。"
    return None, "未识别到体积或可拍信号，需人工判断是否适合短视频。"


def missing_pain_themes(text: str) -> list[str]:
    """列出**没有覆盖**的泰国站痛点主题。

    只报「完全没提到」的 —— 提到任一近义词即算覆盖。
    """
    blob = str(text or "")
    missing = []
    for theme, words in REQUIRED_PAIN_THEMES.items():
        if not any(w in blob for w in words):
            missing.append(theme)
    return missing


def assess_category(
    *, text: str, price_thb: Any = None, category: str | None = None
) -> CategoryAssessment:
    """综合判断一个候选品的类目适配度。纯函数，不触网。"""
    blob = str(text or "")
    resolved = str(category or "").strip() or match_category(blob)

    result = CategoryAssessment(category=resolved)
    status, note = assess_price(price_thb, resolved)
    result.price_status = status
    result.price_band = REFERENCE_PRICE_BANDS.get(resolved)
    result.notes.append(note)

    fit, video_note = assess_video_fit(blob)
    result.video_friendly = fit
    result.video_note = video_note

    result.missing_pain_themes = missing_pain_themes(blob)
    if result.missing_pain_themes:
        result.notes.append(
            "话术未覆盖：" + "、".join(result.missing_pain_themes) + "（泰国站主要痛点）。"
        )

    result.bulky = fit is False
    if not resolved:
        result.notes.append("未能识别类目，价带与类目规则未应用；请人工确认类目。")
    return result


def listing_category_fit(*, text: str, retail_price_thb: Any = None) -> dict[str, Any]:
    """选品报告里的类目适配。纯函数，不触网。

    ``retail_price_thb`` 是泰国站零售价（泰铢）。缺省、空串都视为没给。
    1688 的 ``price_cents``（人民币分）不能传进来冒充零售价。
    """
    provided = retail_price_thb is not None and retail_price_thb != ""
    assessment = assess_category(
        text=text,
        price_thb=retail_price_thb if provided else None,
    )
    data = assessment.as_dict()
    data["band_kind"] = BAND_KIND
    data["band_disclaimer"] = BAND_DISCLAIMER
    if not provided:
        data["price_basis"] = "unavailable"
        data["price_status"] = "unknown"
        notes = [
            note
            for note in data["notes"]
            if "售价无法解析" not in note and "售价不是正数" not in note
        ]
        if NO_RETAIL_PRICE_NOTE not in notes:
            notes.insert(0, NO_RETAIL_PRICE_NOTE)
        data["notes"] = notes
    else:
        price_note = next((note for note in data["notes"] if note.startswith("售价")), "")
        if "无法解析" in price_note or "不是正数" in price_note:
            data["price_basis"] = "unparsed"
        elif data["price_status"] == "unknown":
            data["price_basis"] = "no_band"
        else:
            data["price_basis"] = "retail_thb"
    return data


def render_category_section(rows: list[dict[str, Any]]) -> list[str]:
    """把类目判断渲染成选品报告的一节（md 行）。"""
    lines = ["## 类目适配与价带参考", ""]
    lines.append("> " + BAND_DISCLAIMER)
    lines.append("")
    if not rows:
        lines.append("没有候选行，本节为空。")
        return lines

    lines.append("| 品名 | 类目 | 价带(THB) | 售价判断 | 短视频 | 待补话术 |")
    lines.append("|---|---|---|---|---|---|")
    for row in rows:
        assessment = row.get("category_assessment") or {}
        band = assessment.get("price_band")
        band_text = f"{band[0]}–{band[1]}" if band else "—"
        fit = assessment.get("video_friendly")
        fit_text = "适合" if fit is True else ("不适合" if fit is False else "未判断")
        missing = assessment.get("missing_pain_themes") or []
        lines.append(
            "| {name} | {cat} | {band} | {price} | {fit} | {missing} |".format(
                name=str(row.get("name") or "").replace("|", "/"),
                cat=assessment.get("category") or "—",
                band=band_text,
                price=assessment.get("price_status") or "unknown",
                fit=fit_text,
                missing="、".join(missing) if missing else "—",
            )
        )
    lines.append("")
    return lines
