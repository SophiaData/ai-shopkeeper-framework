"""泰语对外文案的确定性检查。模型自述不算通过。"""

from __future__ import annotations

import re

from shopkeeper.compliance import hits_to_report, scan_supplier_leakage, scan_term_hits

_THAI = re.compile(r"[\u0e00-\u0e7f]")
_ESCALATE = ("退款", "赔偿", "คืนเงิน", "refund", "改地址", "เปลี่ยนที่อยู่")


def review_copy(title_th: str, body_th: str, *, require_thai: bool, extra: str = "") -> dict:
    text = "\n".join((title_th, body_th, extra))
    issues: list[str] = []
    warnings: list[str] = []
    if require_thai and not _THAI.search(f"{title_th}\n{body_th}"):
        issues.append("泰语栏没有泰文字符。")
    if len(title_th) > 255:
        issues.append("泰语标题超过 255 个字符。")
    if not title_th.strip() or not body_th.strip():
        issues.append("泰语标题或正文为空。")
    lowered = text.lower()
    hits = scan_term_hits(text, "TH")
    report = hits_to_report(hits)
    for h in hits:
        loc = f"{h.start}-{h.end}"
        line = f"合规词[{h.severity}/{h.category}]：{h.term}@{loc}"
        if h.severity == "block":
            issues.append(line)
        else:
            warnings.append(line)
    for flag in scan_supplier_leakage(text):
        issues.append(f"供给侧：{flag}")
    escalate = any(hint.lower() in lowered for hint in _ESCALATE)
    if escalate:
        issues.append("退款、赔偿或改地址必须在卖家中心由你处理。此草稿不能点通过，也不会发送。")
    # block 命中 → ok=False → 草稿不可进入审批通过（drafts.decide / orchestrator human_approval）
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "escalate": escalate,
        "compliance": report,
        "hits": [h.to_dict() for h in hits],
    }
