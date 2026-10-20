"""达人建联草稿导出（HANDOFF_PLAN P2-07）。

## 为什么是导出而不是发送

本仓库**不会**给达人发私信、寄样或付款（`SEND_REFUSED`）。批准只改
`drafts.status`。导出 CSV 是给**人**拿去在 TikTok / 联盟后台 / 邮箱里发的 ——
所以这里只做「把草稿整理成人能用的表」，不含任何外呼。

## 两个设计取舍

1. **只用标准库 `csv`**，不引 pandas —— 导出是几十行的活，不值得多一个依赖；
   而且 `csv` 会正确处理引号、换行、逗号，手拼字符串才会出事。
2. **导出是纯函数**（输入草稿列表 → 输出 CSV 文本），不落盘、不触网 ——
   这样它能在测试里被逐字符断言，也便于调用方决定写到哪。
"""

from __future__ import annotations

import csv
import io
from typing import Any

#: 导出列。顺序稳定，便于人工在 Excel 里对照。
EXPORT_COLUMNS: tuple[str, ...] = (
    "draft_id",
    "status",
    "title",
    "creator",
    "body_th",
    "body_en",
    "decision_note",
    "created_at",
    "decided_at",
)

#: 只有这些状态值得导出去发。pending 还没定，rejected 已经否掉。
EXPORTABLE_STATUSES = frozenset({"approved"})


def _cell(value: Any) -> str:
    """单元格取值：None 变空串，其余转字符串。不外抛。"""
    if value is None:
        return ""
    return str(value)


def outreach_row(draft: dict[str, Any]) -> dict[str, str]:
    """把一条草稿规整成一行导出数据。"""
    payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
    return {
        "draft_id": _cell(draft.get("id")),
        "status": _cell(draft.get("status")),
        "title": _cell(draft.get("title")),
        "creator": _cell(payload.get("creator") or payload.get("handle") or ""),
        "body_th": _cell(draft.get("body_th")),
        "body_en": _cell(draft.get("body_en")),
        "decision_note": _cell(draft.get("decision_note")),
        "created_at": _cell(draft.get("created_at")),
        "decided_at": _cell(draft.get("decided_at")),
    }


def export_outreach_csv(
    drafts: list[dict[str, Any]],
    *,
    statuses: frozenset[str] = EXPORTABLE_STATUSES,
) -> str:
    """把草稿导成 CSV 文本。

    默认**只导已批准的** —— 未批准的草稿发出去等于绕过了审批闸门。
    传 `statuses=...` 可覆盖（例如导出全部用于盘点）。

    CSV 带表头；用 `\\r\\n` 换行以兼容 Excel。
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS), lineterminator="\r\n")
    writer.writeheader()
    for draft in drafts:
        if not isinstance(draft, dict):
            continue
        if statuses and str(draft.get("status") or "") not in statuses:
            continue
        writer.writerow(outreach_row(draft))
    return buffer.getvalue()


def export_outreach_filename(*, shop_id: str | None = None) -> str:
    """建议的下载文件名。shop 为空时不加后缀，避免出现 `-None`。"""
    shop = str(shop_id or "").strip()
    return f"outreach-drafts-{shop}.csv" if shop else "outreach-drafts.csv"
