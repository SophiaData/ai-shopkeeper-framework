"""TikTok 结算单导入与对账（HANDOFF_PLAN P2-10）。

## 对账线

对应 SCHEMA §4.7 的第四条线 `ledger_vs_settlement`：
把平台结算单里**实际结算了多少**，与本地台账记的**应该结算多少**比，
差异写进 `recon_diffs`（`critical` 阻断人工确认，`warn` 只提示）。

## ⚠️ 关于 CSV 列名：刻意做成宽容映射，而不是写死表头

**本仓库无法核对 TikTok 结算单的真实导出列名**（文档页是 JS 登录墙、web 检索不可用）。
按 AGENTS 规则 4「不编造」，我没有臆造一套官方表头，而是：

1. 用**多个候选列名**做大小写/空格无关的匹配（`order_id` / `Order ID` / `订单号` …）
2. **匹配不到必需列时明确报错并列出实际表头**，让用户知道该改哪里
3. 金额按**分**存储（与全仓库口径一致），并处理 `12.34` / `1234` / `฿12.34` 等写法

这样即使真实表头与我的候选不同，用户也能靠报错信息自行补上候选，
而不会拿到一个**静默算错**的结果。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

#: 对账线名（与 SCHEMA §4.7 一致）。
RECON_LINE = "ledger_vs_settlement"

#: 订单号列的候选名。匹配时忽略大小写、空格、下划线。
_ORDER_KEYS = (
    "order_id",
    "orderid",
    "order no",
    "orderno",
    "订单号",
    "订单编号",
    "平台订单号",
)

#: 结算金额列的候选名。
#: 结算金额列的候选名。
#:
#: **刻意不含裸 `amount` / `金额`** —— 它们会命中 `Order Amount`（订单毛额）、
#: `Shipping Fee Amount`（运费）等**不是结算净额**的列，把毛额当净额会静默算错账。
#: 只用明确的「结算/打款」措辞。
_AMOUNT_KEYS = (
    "settlement_amount",
    "settlementamount",
    "settlement amount",
    "payout_amount",
    "payoutamount",
    "payout amount",
    "net_amount",
    "结算金额",
    "实际结算",
    "结算净额",
    "打款金额",
)

#: 币种列候选（可选）。
_CURRENCY_KEYS = ("currency", "币种", "货币")


class SettlementImportError(ValueError):
    """结算单导入失败（表头缺失、金额无法解析等）。"""


@dataclass
class SettlementLine:
    order_id: str
    amount_minor: int
    currency: str = ""
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class SettlementImportResult:
    lines: list[SettlementLine]
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": len(self.lines),
            "skipped": self.skipped,
            "warnings": list(self.warnings),
        }


def _norm(key: str) -> str:
    # 去掉 BOM：Excel 另存为 CSV 时会在首个表头前加 U+FEFF，
    # 不剥掉的话「订单号」会因为前面多了个隐形字符而匹配不上。
    return (
        str(key or "")
        .replace("\ufeff", "")
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
    )


def _find_column(header: list[str], candidates: tuple[str, ...]) -> str | None:
    """在表头里找一个候选列。忽略大小写与分隔符差异。"""
    normalized = {_norm(h): h for h in header}
    for cand in candidates:
        hit = normalized.get(_norm(cand))
        if hit is not None:
            return hit
    # 容错匹配：只接受**前缀**或**括号后缀**，不接受任意位置子串。
    #
    # 为什么不能放宽成「任意子串」：候选 `amount` 会命中 `Order Amount`（毛额）、
    # `Shipping Fee Amount`（运费）—— 把毛额当结算净额会**静默算错**，比报错更危险。
    # 真实表头常见的变体是「结算金额(THB)」「Settlement Amount (THB)」这类后缀，
    # 前缀/括号后缀已能覆盖，且不会误伤。
    hits: list[str] = []
    for norm_key, original in normalized.items():
        for cand in candidates:
            key = _norm(cand)
            if norm_key == key or norm_key.startswith(key + " ") or norm_key.startswith(key + "("):
                hits.append(original)
                break
    if len(hits) > 1:
        # 多个列都像结算金额 —— **不猜**，让用户明确指定。
        raise SettlementImportError(
            f"结算单里有多个列都像结算金额：{', '.join(hits)}。"
            "无法确定用哪一个，请只保留真正的那一列后重试。"
        )
    return hits[0] if hits else None


def parse_amount_minor(raw: Any) -> int | None:
    """把金额解析成**分**。

    容忍：`12.34` → 1234、`1234` → 123400、`฿12.34` → 1234、
    `1,234.56` → 123456、`-12.34` → -1234（退款/罚款可以是负的）。
    空/无法解析返回 None（**不填 0** —— 0 是「结算了 0 元」，与「没数据」不同）。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    for junk in ("฿", "$", "¥", "THB", "thb", ",", " "):
        text = text.replace(junk, "")
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return int(round(value * 100))


def import_settlement_csv(text: str) -> SettlementImportResult:
    """把结算单 CSV 文本解析成行。

    找不到必需列时抛 `SettlementImportError`，**并把实际表头列出来** ——
    让用户能自己判断该用哪个列名，而不是对着「导入失败」猜。
    """
    if not str(text or "").strip():
        raise SettlementImportError("结算单为空。")

    reader = csv.DictReader(io.StringIO(text))
    header = list(reader.fieldnames or [])
    if not header:
        raise SettlementImportError("结算单没有表头，无法识别列。")

    order_col = _find_column(header, _ORDER_KEYS)
    amount_col = _find_column(header, _AMOUNT_KEYS)
    missing = []
    if order_col is None:
        missing.append("订单号")
    if amount_col is None:
        missing.append("结算金额")
    if missing:
        raise SettlementImportError(
            f"结算单缺少必需列：{'、'.join(missing)}。"
            f"实际表头为：{', '.join(header)}。"
            "请把实际列名加入本模块的候选列表后重试。"
        )

    currency_col = _find_column(header, _CURRENCY_KEYS)

    result = SettlementImportResult(lines=[])
    for row in reader:
        order_id = str(row.get(order_col) or "").strip()
        if not order_id:
            result.skipped += 1
            continue
        amount = parse_amount_minor(row.get(amount_col))
        if amount is None:
            result.skipped += 1
            result.warnings.append(f"订单 {order_id} 的结算金额无法解析，已跳过。")
            continue
        result.lines.append(
            SettlementLine(
                order_id=order_id,
                amount_minor=amount,
                currency=str(row.get(currency_col) or "").strip() if currency_col else "",
                raw={k: str(v or "") for k, v in row.items() if k},
            )
        )
    return result


def reconcile_against_ledger(
    store: Any,
    lines: list[SettlementLine],
    *,
    ledger_amounts: dict[str, int],
    shop_id: str | None = None,
) -> dict[str, Any]:
    """把结算单与本地台账逐单比对，差异写 `recon_diffs`。

    ``ledger_amounts`` 是「本地台账认为该订单应结算多少（分）」。
    差异为 0 的不记录（否则对账表会被无差异的行淹没）。

    返回统计；**不修改台账** —— 修正要走 `ledger_correction` 审批（SCHEMA §4.6）。
    """
    matched = 0
    diffs_recorded = 0
    missing_in_ledger: list[str] = []
    missing_in_settlement: list[str] = []

    # 先按订单号**聚合**：结算单常按「订单行」导出（一个订单多个 SKU 就是多行）。
    # 直接逐行比对会把「合计正确」误报成 critical 差异 —— 卖家会去追一个不存在的账差，
    # 而真正的账差反而被这堆噪声淹没。聚合成「每单结算总额」再比，才是同一层级的比较。
    totals: dict[str, int] = {}
    for line in lines:
        totals[line.order_id] = totals.get(line.order_id, 0) + line.amount_minor

    seen: set[str] = set()
    for order_id, amount_minor in totals.items():
        line = SettlementLine(order_id=order_id, amount_minor=amount_minor)
        seen.add(line.order_id)
        expected = ledger_amounts.get(line.order_id)
        if expected is None:
            missing_in_ledger.append(line.order_id)
            store.insert_recon_diff(
                {
                    "shop_id": shop_id,
                    "recon_line": RECON_LINE,
                    "ref_type": "order",
                    "ref_id": line.order_id,
                    "expected": None,
                    "actual": line.amount_minor,
                    "diff_amount": None,
                    "severity": "warn",
                    "resolution": "本地台账无此订单的结算记录，需人工核对。",
                }
            )
            diffs_recorded += 1
            continue
        if expected == line.amount_minor:
            matched += 1
            continue
        diff = line.amount_minor - expected
        store.insert_recon_diff(
            {
                "shop_id": shop_id,
                "recon_line": RECON_LINE,
                "ref_type": "order",
                "ref_id": line.order_id,
                "expected": expected,
                "actual": line.amount_minor,
                "diff_amount": diff,
                # 金额不符一律 critical：可能是费率、退款或汇率算错，不该只提示。
                "severity": "critical",
                "resolution": "结算金额与台账不一致，须人工核对后再走 ledger_correction 冲正。",
            }
        )
        diffs_recorded += 1

    for order_id in ledger_amounts:
        if order_id not in seen:
            missing_in_settlement.append(order_id)

    return {
        "recon_line": RECON_LINE,
        "matched": matched,
        "diff_recorded": diffs_recorded,
        "missing_in_ledger": missing_in_ledger,
        "missing_in_settlement": missing_in_settlement,
        "total_lines": len(lines),
        "note": "差异只记录，不自动冲正；修正须走 ledger_correction 审批。",
    }
