#!/usr/bin/env python3
"""把卖家自己整理的候选 CSV 收成选品短名单。

只读本地文件，不访问网络，不登录卖家中心，也不生成采购单。
Coze 选品 Bot 的提示词在 prompts/product-research.md。把本脚本的输出贴进去即可。

当前限制：不做趋势抓取，不调用 COZE_API_TOKEN。那个变量可以空着。
TODO: 若以后要交给 Coze，由人在 Coze 界面粘贴，或单独写一个明确开关的客户端；
不要在这个摘要脚本里默默发 HTTP。

环境变量（均可空）：
  RESEARCH_INPUT_PATH   未传 --input 时使用
  RESEARCH_OUTPUT_PATH  未传 --output 时使用；都空着则打印到标准输出
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Any
from pathlib import Path

from shopkeeper.compliance import scan_terms

REQUIRED = ("name", "pain", "scene", "diff", "price_band")

ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "品名", "sku", "sku_name", "产品"),
    "pain": ("pain", "痛点", "pain_point"),
    "scene": ("scene", "场景"),
    "diff": ("diff", "差异", "差异点", "differentiation"),
    "price_band": ("price_band", "价带", "建议定价区间", "价格带"),
    "supply_status": ("supply_status", "供货", "可供货"),
    "risk_note": ("risk_note", "风险", "风险备注"),
    "keyword": ("keyword", "关键词"),
    "notes": ("notes", "备注"),
}

SCENE_TAGS: dict[str, tuple[str, ...]] = {
    "雨季防潮": ("防潮", "潮湿", "雨季", "密封"),
    "小户型": ("小户型", "小空间", "公寓", "窄"),
    "租房免打孔": ("租房", "免打孔", "无痕", "不打孔"),
    "厨房": ("厨房",),
    "衣柜": ("衣柜", "衣架", "衣物"),
    "浴室": ("浴室", "卫生间"),
}

SHORTLIST_LIMIT = 10


class DigestError(Exception):
    """输入不满足本地摘要的契约。"""


def refuse_remote(path: str) -> None:
    lowered = path.strip().lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        raise DigestError("本脚本不抓取网页。请把你手工整理或官方导出的表格存成本地 CSV。")


def normalize_header(name: str) -> str:
    return name.strip().lstrip("\ufeff")


def canonical_rows(raw_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not raw_rows:
        raise DigestError("CSV 没有数据行。")
    headers = [normalize_header(key) for key in raw_rows[0].keys() if key is not None]
    header_set = set(headers)
    resolved: dict[str, str] = {}
    for canonical, names in ALIASES.items():
        for candidate in names:
            if candidate in header_set:
                resolved[canonical] = candidate
                break
    missing = [name for name in REQUIRED if name not in resolved]
    if missing:
        raise DigestError(
            "缺少列：" + "、".join(missing) + "。至少需要品名、痛点、场景、差异点、价带。"
        )
    rows: list[dict[str, str]] = []
    for raw in raw_rows:
        item = {
            canonical: (raw.get(source) or "").strip()
            for canonical, source in resolved.items()
        }
        if not any(item.get(field) for field in REQUIRED):
            continue
        rows.append(item)
    if not rows:
        raise DigestError("CSV 没有有效数据行。")
    return rows


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise DigestError(f"找不到文件：{path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DigestError("CSV 没有表头。")
        return list(reader)


def scene_tags(text: str) -> list[str]:
    found = [label for label, words in SCENE_TAGS.items() if any(word in text for word in words)]
    return found or ["场景证据不足"]


def caution_hits(text: str) -> list[str]:
    """词表只在 shopkeeper.compliance。站点按 DESIGN §12.1 使用 TH。"""
    return scan_terms(text, "TH")


def cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def row_text(row: dict[str, str]) -> str:
    return " ".join(row.get(key, "") for key in (*REQUIRED, "supply_status", "risk_note", "keyword", "notes"))


def is_fixture(rows: list[dict[str, str]]) -> bool:
    return any("fixture_not_live" in row.get("notes", "") for row in rows)


def _category_lines(rows: list[dict[str, str]]) -> list[str]:
    """按 P2-08 的类目规则给短名单加一节判断。

    价带取行里的 `price_band` 文本（可能是 `99-199` 这种），尽力解析出数字；
    解析不出就不做价带判断（**不猜**），只出短视频适配与话术覆盖。
    """
    from shopkeeper.scout.category_rules import assess_category, render_category_section

    enriched: list[dict[str, Any]] = []
    for row in rows:
        text = row_text(row)
        assessment = assess_category(text=text, price_thb=_first_price(row.get("price_band")))
        enriched.append({"name": row.get("name") or "", "category_assessment": assessment.as_dict()})
    return render_category_section(enriched)


def _first_price(raw: str | None) -> float | None:
    """从 `99`、`99-199`、`99~199`、`฿99`、`฿1,299` 里取出第一个数字。取不到返回 None。

    **必须先剥千分位逗号**：朴素的数字正则会只匹配到 `1` 而非 `1299`，
    于是 1,299 THB 的商品被判成「低于价带 99–399，请确认能否覆盖成本」——
    **结论完全反了**，会让人给高价品做无意义的成本核查。
    """
    import re

    text = re.sub(r"[,\s\u00a0]", "", str(raw or ""))  # 千分位与空格
    text = text.replace("฿", "").replace("$", "").replace("¥", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def render(rows: list[dict[str, str]], source: str) -> str:
    lines: list[str] = ["# 选品短名单", ""]
    if is_fixture(rows):
        lines.append("示例数据：表内内容是仓库自带的假数据，用来检查脚本能否跑通，不能当成选品结论。")
        lines.append("")
    lines.append(f"数据来源：本地文件 `{source}`。本摘要没有访问 TikTok、供应商网站或卖家中心。")
    lines.append("")
    lines.append("## 人工卡点")
    lines.append("")
    lines.append("- 登录卖家中心确认类目要求。")
    lines.append("- 确认供应商可以供货。未写「可供货」的行不要直接开新品。")
    lines.append("- 采购和付款由你本人完成。")
    lines.append("")
    primary = rows[:SHORTLIST_LIMIT]
    rest = rows[SHORTLIST_LIMIT:]
    # P2-08：把手册 §2.1 的类目判断变成报告里可执行的一节（价带 / 短视频适配 / 待补话术）。
    lines.extend(_category_lines(primary))
    lines.append(f"## 短名单（{len(primary)}）")
    lines.append("")
    lines.append("| 品名 | 痛点 | 场景标签 | 差异点 | 价带 | 供货 | 主图方向 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in primary:
        tags = "、".join(scene_tags(row_text(row)))
        supply = row.get("supply_status") or "未填写"
        image = f"拍{row['scene']}的前后对比，画面里放尺码参照物"
        lines.append(
            "| {name} | {pain} | {tags} | {diff} | {price} | {supply} | {image} |".format(
                name=cell(row["name"] or "未命名"),
                pain=cell(row["pain"]),
                tags=cell(tags),
                diff=cell(row["diff"]),
                price=cell(row["price_band"]),
                supply=cell(supply),
                image=cell(image),
            )
        )
    lines.append("")
    if rest:
        lines.append(f"其余 {len(rest)} 条先作为备选，短名单保持在 {SHORTLIST_LIMIT} 个以内。")
        lines.append("")
    lines.append("## 风险提醒")
    lines.append("")
    hits = 0
    for row in primary:
        found = caution_hits(row_text(row))
        supply = row.get("supply_status", "")
        notes: list[str] = []
        if found:
            notes.append("文案出现「" + "」「".join(found) + "」，没有依据就不要写进上架文案")
        if supply and supply != "可供货":
            notes.append(f"供货状态为「{supply}」")
        if not supply:
            notes.append("供货状态未填写")
        if notes:
            hits += 1
            lines.append(f"- {row['name'] or '未命名'}：" + "；".join(notes) + "。")
    if hits == 0:
        lines.append("- 短名单里没有命中内置敏感说法。这只是词表初筛，类目合规仍要你在卖家中心确认。")
    lines.append("")
    lines.append("下一步：把本页贴进 Coze，使用 prompts/product-research.md。")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把本地候选表整理成选品短名单。不访问网络。")
    parser.add_argument("--input", default=os.environ.get("RESEARCH_INPUT_PATH", ""))
    parser.add_argument("--output", default=os.environ.get("RESEARCH_OUTPUT_PATH", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input:
        print("请用 --input 指定本地 CSV，或设置 RESEARCH_INPUT_PATH。", file=sys.stderr)
        return 2
    try:
        refuse_remote(args.input)
        rows = canonical_rows(load_csv(Path(args.input)))
        report = render(rows, args.input)
    except DigestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
