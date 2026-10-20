"""上架前人工核对预览（HANDOFF_PLAN P0-05）。

审批人在点「通过」之前，应当看到**即将发给 TikTok 的东西**：最终载荷（脱敏）、
合规检查结果、售价与毛利、以及写通道是否武装。

关键约束：**预览与实际发布必须出自同一个构造函数**。否则预览只是装饰，会与实际
发出的载荷悄悄分叉。因此本模块直接复用 `workflow.gate._listing_payload`，并用
`approvals.payload_hash` 给出与发布时相同的哈希供人核对。

本模块**不发起任何外呼**，也不改任何状态。
"""

from __future__ import annotations

from typing import Any

from shopkeeper.compliance import scan_terms
from shopkeeper.workflow.approvals import payload_hash
from shopkeeper.workflow.gate import _listing_payload

# 预览里不应出现的敏感键（供给侧/凭据）。命中即用占位替换，避免直接泄露给别人。
_REDACT_KEYS = ("app_secret", "access_token", "refresh_token", "api_key", "secret", "password")

# 正文预览截断长度：够看清写了什么，又不至于把整份文案塞进审批页。
_BODY_LIMIT = 600


def _redact(value: Any) -> Any:
    """递归打码敏感键；非 dict/list 原样返回。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(bad in lowered for bad in _REDACT_KEYS):
                out[key] = "[REDACTED]"
            elif lowered in {"image_b64", "images_b64"} and isinstance(item, str) and item:
                out[key] = f"[base64 {len(item)} chars]"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _trim(text: Any, limit: int = _BODY_LIMIT) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + f"…（共 {len(s)} 字）"


def build_publish_preview(
    store: Any,
    approval: dict[str, Any],
    *,
    write_armed: bool = False,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造审批详情页用的预览。只读：不落库、不外呼、不改状态。

    返回结构：
      - `payload`：与实际发布同源（`_listing_payload`）+ 相同 `payload_hash`
      - `copy`：泰/英文案（截断）
      - `compliance`：合规命中（block/warn 分级）
      - `pricing`：售价与毛利明细（若可算）
      - `write_armed`：TTS 写通道是否打开
      - `preview_consistent`：审批时登记的身份字段（标题）是否仍与草稿一致
      - `warnings`：审批人动手前该知道的事（缺图、缺类目、合规命中…）
    """
    entity_id = str(approval.get("entity_id") or "")
    draft = store.get_draft(entity_id) if entity_id else None

    warnings: list[str] = []
    result: dict[str, Any] = {
        "approval_id": approval.get("id"),
        "entity_id": entity_id,
        "action_type": approval.get("action_type"),
        "status": approval.get("status"),
        "title": "",
        "copy": {"title_th": "", "title_en": "", "body_th": "", "body_en": ""},
        "images": [],
        "payload": {},
        "payload_hash": "",
        "approved_payload_hash": str(approval.get("payload_hash") or ""),
        "reviewed_title": "",
        "preview_consistent": False,
        "compliance": {"hits": [], "blocked": False},
        "pricing": pricing or {},
        "write_armed": bool(write_armed),
        "warnings": warnings,
    }

    if draft is None:
        warnings.append("找不到关联草稿，无法预览载荷。")
        return result

    payload = _listing_payload(draft)
    result["payload"] = _redact(payload)
    result["payload_hash"] = payload_hash(payload)
    # 注意：审批单登记的 `payload_hash` 与这里算的不是同一个东西——审批用的是
    # 「身份载荷」(draft_id/title/listing_version/qa_ok，见 approvals.ensure_listing_approval)，
    # 发布用的是「发布载荷」(gate._listing_payload，含正文与 payload_digest)。
    # 两者刻意不同，不能直接比哈希。这里改为核对**审批时登记的身份字段**是否仍然成立，
    # 这才是「审批后被改过」的真实信号。
    reviewed_title = str(approval.get("payload", {}).get("title") or "") if isinstance(approval.get("payload"), dict) else ""
    current_title = str(draft.get("title") or "")
    result["reviewed_title"] = reviewed_title
    result["preview_consistent"] = (not reviewed_title) or reviewed_title == current_title
    if reviewed_title and reviewed_title != current_title:
        warnings.append(
            f"草稿标题在审批后被改过（审批时「{reviewed_title}」→ 现在「{current_title}」），"
            "请重新审批，不要直接通过。"
        )

    result["title"] = str(draft.get("title") or "")
    for key in ("title_th", "title_en", "body_th", "body_en"):
        value = draft.get(key) if key != "title_th" else (draft.get("title") or "")
        result["copy"][key] = _trim(value)

    qa = draft.get("qa") if isinstance(draft.get("qa"), dict) else {}
    result["qa"] = {
        "ok": bool(qa.get("ok")),
        "issues": list(qa.get("issues") or []),
        "escalate": bool(qa.get("escalate")),
    }
    if not qa.get("ok"):
        warnings.append("质检未通过——按现有规则不能点通过。")

    inp = (draft.get("payload") or {}).get("input") or {}
    images = inp.get("images") or []
    if isinstance(images, list):
        result["images"] = [str(p) for p in images if str(p).strip()]
    if inp.get("image_b64"):
        result["images"] = result["images"] or ["[inline base64]"]
    if not result["images"]:
        warnings.append("没有主图；真写上架会被平台拒绝。")
    if not str(inp.get("category_id") or "").strip():
        warnings.append("缺 category_id。")
    if not str(inp.get("warehouse_id") or "").strip():
        warnings.append("缺 warehouse_id。")

    text = "\n".join(
        str(x)
        for x in (
            draft.get("title"),
            draft.get("body_th"),
            draft.get("body_en"),
        )
        if x
    )
    hits = scan_terms(text, "TH") if text else []
    result["compliance"] = {"hits": list(hits), "blocked": bool(hits)}
    if hits:
        warnings.append(f"合规词命中 {len(hits)} 处，按 §7.5 不应上架。")

    if not write_armed:
        warnings.append("TTS_WRITE_ENABLED 未打开：即使通过也不会真上架。")

    return result
