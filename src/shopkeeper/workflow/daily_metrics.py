#!/usr/bin/env python3
"""把卖家导出的指标 CSV 收成一页纸日报。

只读本地文件。数字缺失就保持缺失，不估算 GMV，不改价，不调用广告接口。
复盘 Bot 的提示词在 prompts/daily-report.md。

当前限制：本模块只读本地 CSV。店铺订单的只读客户端在 adapters/tiktok_shop.py；没有凭据时工作台保持空列表。上架、发货、付款、退款不在这里执行。

环境变量（均可空）：
  DAILY_METRICS_PATH    未传 --input 时使用
  DAILY_REPORT_OUTPUT   未传 --output 时使用；都空着则打印到标准输出
  WORKFLOW_TIMEZONE     只作为日报标题里的时区标签，默认 Asia/Bangkok，不做时区换算
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date
from pathlib import Path

METRIC_FIELDS = ("impressions", "clicks", "orders", "returns", "ad_spend_thb", "low_stock_count", "cs_tickets")
ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日期"),
    "impressions": ("impressions", "曝光"),
    "clicks": ("clicks", "点击"),
    "orders": ("orders", "订单"),
    "returns": ("returns", "退货"),
    "ad_spend_thb": ("ad_spend_thb", "广告花费", "ad_spend"),
    "low_stock_count": ("low_stock_count", "低库存", "低库存sku数"),
    "cs_tickets": ("cs_tickets", "客服", "客服条数"),
    "notes": ("notes", "备注"),
}


class ReportError(Exception):
    """输入不满足日报契约。"""


def refuse_remote(path: str) -> None:
    lowered = path.strip().lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        raise ReportError("本脚本不抓取网页，也不登录卖家中心。请使用你下载到本地的官方导出 CSV。")


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ReportError(f"找不到文件：{path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ReportError("CSV 没有表头。")
        rows = list(reader)
    if not rows:
        raise ReportError("CSV 没有数据行。")
    return rows


def resolve_headers(sample: dict[str, str]) -> dict[str, str]:
    headers = {key.strip().lstrip("\ufeff") for key in sample.keys() if key is not None}
    resolved: dict[str, str] = {}
    for canonical, names in ALIASES.items():
        for candidate in names:
            if candidate in headers:
                resolved[canonical] = candidate
                break
    if "date" not in resolved:
        raise ReportError("缺少日期列（date 或 日期）。")
    return resolved


def parse_number(raw: str) -> float | None:
    text = raw.strip().replace(",", "")
    if text == "":
        return None
    return float(text)


def normalize(raw_rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], list[str]]:
    resolved = resolve_headers(raw_rows[0])
    problems: list[str] = []
    rows: list[dict[str, object]] = []
    for index, raw in enumerate(raw_rows, start=2):
        item: dict[str, object] = {"date": (raw.get(resolved["date"]) or "").strip(), "notes": ""}
        if "notes" in resolved:
            item["notes"] = (raw.get(resolved["notes"]) or "").strip()
        for field in METRIC_FIELDS:
            source = resolved.get(field)
            if source is None:
                item[field] = None
                continue
            raw_value = raw.get(source) or ""
            try:
                item[field] = parse_number(raw_value)
            except ValueError:
                item[field] = None
                problems.append(f"第 {index} 行的 {field} 不是数字，已忽略，不替换成估计值。")
        rows.append(item)
    return rows, problems


def sort_rows(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], str]:
    def key(pair: tuple[int, dict[str, object]]) -> tuple[int, object, int]:
        index, row = pair
        try:
            return (0, date.fromisoformat(str(row["date"])), index)
        except ValueError:
            return (1, index, 0)

    ordered_pairs = sorted(enumerate(rows), key=key)
    note = ""
    if any(key(pair)[0] == 1 for pair in ordered_pairs):
        note = "部分日期不是 YYYY-MM-DD，能解析的日期在前，其余保持原表顺序。"
    return [row for _, row in ordered_pairs], note


def show_value(value: object) -> str:
    if value is None or value == "":
        return "未提供"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def direction(current: object, previous: object) -> str | None:
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        return None
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


def pad(items: list[str], filler: str) -> list[str]:
    result = list(items[:3])
    while len(result) < 3:
        result.append(filler)
    return result


def build_report(raw_rows: list[dict[str, str]], source: str, timezone_label: str) -> str:
    rows, parse_notes = normalize(raw_rows)
    ordered, date_note = sort_rows(rows)
    latest = ordered[-1]
    previous = ordered[-2] if len(ordered) > 1 else None
    fixture = any("fixture_not_live" in str(row.get("notes", "")) for row in ordered)

    goods: list[str] = []
    issues: list[str] = list(parse_notes)
    if date_note:
        issues.append(date_note)
    if previous is None:
        issues.append("只有一行数据，没有昨日对照，不判断涨跌。")
    else:
        orders_dir = direction(latest.get("orders"), previous.get("orders"))
        returns_dir = direction(latest.get("returns"), previous.get("returns"))
        clicks_dir = direction(latest.get("clicks"), previous.get("clicks"))
        if orders_dir == "up":
            goods.append("订单高于表中的前一行。这是方向，不是增长率结论。")
        if returns_dir == "down":
            goods.append("退货低于表中的前一行。")
        if clicks_dir == "up" and orders_dir != "down":
            goods.append("点击高于表中的前一行，且订单没有下降。")
        if orders_dir == "down":
            issues.append("订单低于表中的前一行。")
        if returns_dir == "up":
            issues.append("退货高于表中的前一行。")

    low_stock = latest.get("low_stock_count")
    if isinstance(low_stock, (int, float)) and low_stock > 0:
        issues.append(f"最新一行低库存数量为 {low_stock:g}。请对照卖家中心和在途采购。")
    spend = latest.get("ad_spend_thb")
    orders = latest.get("orders")
    if isinstance(spend, (int, float)) and spend > 0 and orders == 0:
        issues.append("最新一行有广告花费，订单为 0。是否继续投放由你决定。")

    missing = [field for field in ("impressions", "clicks", "orders", "returns") if latest.get(field) is None]
    if missing:
        issues.append("最新一行缺少：" + "、".join(missing) + "。请从卖家中心导出后填入，脚本不补数字。")

    actions = [
        "主实验只选一个：改一版标题，或换一个 Hook，或补一条视频。发布前由你检查泰语。",
        "登录卖家中心处理待发货和售后。这个脚本看不到实时订单。",
    ]
    if isinstance(low_stock, (int, float)) and low_stock > 0:
        actions.append("核对低库存和在途。采购和付款由你本人完成。")
    else:
        actions.append("今天不改价、不加广告。若要改价或投放，由你在卖家中心手动操作。")

    lines: list[str] = ["# 每日复盘", ""]
    if fixture:
        lines.append("示例数据：表内数字是仓库自带的假数据，用来检查脚本能否跑通，不能当成店铺成绩。")
        lines.append("")
    lines.append(f"时区标签：{timezone_label}。脚本不做时区换算。")
    lines.append(f"数据来源：本地文件 `{source}`。未连接 TikTok Shop API。")
    lines.append("")
    lines.append("## 最新一行（原样转写，缺的就是缺的）")
    lines.append("")
    for field in ("date", *METRIC_FIELDS):
        lines.append(f"- {field}：{show_value(latest.get(field))}")
    lines.append("")
    lines.append("## 3 个好消息")
    lines.append("")
    for index, item in enumerate(pad(goods, "这条没有足够数字，不编造好消息。"), start=1):
        lines.append(f"{index}. {item}")
    lines.append("")
    lines.append("## 3 个问题")
    lines.append("")
    for index, item in enumerate(pad(issues, "没有更多从表里能确定的问题。"), start=1):
        lines.append(f"{index}. {item}")
    lines.append("")
    lines.append("## 明日 3 个动作")
    lines.append("")
    for index, item in enumerate(actions, start=1):
        lines.append(f"{index}. {item}")
    lines.append("")
    lines.append("## 人工卡点")
    lines.append("")
    lines.append("改价、加广告、下架、发货、退款都由你在卖家中心点击。本页不给出新的售价。")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把本地指标 CSV 整理成日报。不访问网络。")
    parser.add_argument("--input", default=os.environ.get("DAILY_METRICS_PATH", ""))
    parser.add_argument("--output", default=os.environ.get("DAILY_REPORT_OUTPUT", ""))
    parser.add_argument(
        "--timezone-label",
        default=os.environ.get("WORKFLOW_TIMEZONE", "Asia/Bangkok"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input:
        print("请用 --input 指定本地 CSV，或设置 DAILY_METRICS_PATH。", file=sys.stderr)
        return 2
    try:
        refuse_remote(args.input)
        report = build_report(load_csv(Path(args.input)), args.input, args.timezone_label)
    except ReportError as exc:
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
