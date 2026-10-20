"""达人建联草稿队列。

批准和拒绝只改 ``drafts.status``。没有发送动作，不写 ``external_writes``，
不调用平台适配器。
"""

from __future__ import annotations

from typing import Any

from shopkeeper.orchestrator.outreach import DRAFT_KIND
from shopkeeper.workflow.drafts import DraftError, decide_draft
from shopkeeper.workflow.store import WorkflowStore

QUEUE_NOTE = "批准只改状态，不发送私信、不寄样、不付款。"
SEND_REFUSED = (
    "达人建联草稿不会发送。请你本人在 TikTok、联盟或邮箱中发送。"
    "寄样地址和运费由你确认。佣金和付款由你完成，本草稿不是合同。"
)


def list_outreach_drafts(store: WorkflowStore, *, shop_id: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for draft in store.list_drafts(shop_id=shop_id):
        if draft.get("kind") != DRAFT_KIND:
            continue
        view = dict(draft)
        view["sent"] = False
        view["auto_send"] = False
        view["dispatched"] = False
        rows.append(view)
    return rows


def decide_outreach_draft(
    store: WorkflowStore,
    draft_id: str,
    action: str,
    note: str = "",
    *,
    decided_by: str = "console",
) -> dict[str, Any]:
    draft = store.get_draft(draft_id)
    if draft is None:
        raise DraftError("草稿不存在。")
    if draft.get("kind") != DRAFT_KIND:
        raise DraftError("只能决定达人建联草稿。")
    saved = decide_draft(store, draft_id, action, note, decided_by=decided_by)
    saved["sent"] = False
    saved["published"] = False
    saved["dispatched"] = False
    saved["auto_send"] = False
    return {
        "draft": saved,
        "sent": False,
        "published": False,
        "dispatched": False,
        "auto_send": False,
        "note": QUEUE_NOTE,
    }
