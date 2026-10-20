"""生成泰语 + 英语草稿，并放进人工队列。

通过只改变状态，不调用平台写接口。上架须再走 `gate.publish_listing`：
仅八态审批终态批准（approved）的 listing 才会调用 PlatformAdapter.create_listing。

草稿 approve/reject API 薄封装到 SCHEMA approvals 八态；队列字段保持兼容投影。
权威规则见 DESIGN §6 / SCHEMA §3.4。本模块无「跳过本次」。
"""

from __future__ import annotations

import json

from shopkeeper.domain.approval_state import ApprovalEvent
from shopkeeper.security.redaction import redact_spans
from shopkeeper.site_profile import SiteProfile, active_site_profile
from shopkeeper.workflow.providers import ChatProvider, parse_model_json, render_mock
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.thai_qa import review_copy


def draft_system(profile: SiteProfile | None = None) -> str:
    """草稿系统提示。店铺名读画像；默认画像与接线前逐字相同。"""
    stem = (profile or active_site_profile()).display_stem
    return (
        f"你是 TikTok Shop {stem}的草稿助理。"
        "只使用用户给出的事实。不要编造销量、防水认证、食品级、承重数字或退款金额。"
        "退款、赔偿、改地址要明确写需要人工处理。"
        "只返回 JSON："
        '{"title_th":"","title_en":"","body_th":"","body_en":""}'
    )


class DraftError(Exception):
    pass


def generate_drafts(
    store: WorkflowStore,
    provider: ChatProvider,
    kind: str,
    items: list[dict[str, str]],
    *,
    shop_id: str | None = None,
) -> list[dict]:
    if kind not in {"listing", "video_script", "customer_reply"}:
        raise DraftError("kind 只能是 listing、video_script 或 customer_reply。")
    if not items:
        raise DraftError("至少提供一条输入。")

    # ⚠️ **这条路径此前既不检查也不记账 LLM 花费**。
    #
    # 编排器（`orchestrator/llm.py`）走 `check_budget` + `record_llm_usage`，
    # 而 `/api/workflow/drafts/generate` 走本函数 —— 用**同一个 LLM 网关**，
    # 却完全没有预算门与计量。后果有两层：
    #   1. `LLM_DAILY_BUDGET_USD` 拦不住这条路径（实测：预算设 0、编排器拒绝，
    #      本函数**照常执行**）
    #   2. 后台「今日剩余额度」只读 `orch_llm_usage`，这条路径的消耗**不计入**，
    #      于是看到的是「花了 0，额度满」—— 一个会误导人的数字
    #
    # 这里补上**与编排器同一套**的预算检查（同一个 `check_budget`、同一张用量表），
    # 使两条路径口径一致。只读不写：真正记账在 `_one` 成功后做。
    from shopkeeper.orchestrator.budget import check_budget, daily_budget_usd
    from shopkeeper.orchestrator.store import OrchestratorStore

    if daily_budget_usd() is not None:
        try:
            spent = float(OrchestratorStore(store).spent_usd_today())
        except Exception:  # noqa: BLE001 - 读不到用量时不因此阻断生成
            spent = 0.0
        gate = check_budget(spent, env=None, emit=True, run_id="")
        if not gate.get("ok", True):
            raise DraftError(
                "LLM 日预算已用尽，草稿生成已拒绝"
                f"（已用 {gate.get('spent')} / 上限 {gate.get('limit')} USD）。"
                "请调高 LLM_DAILY_BUDGET_USD 或等次日重置。"
            )

    created: list[dict] = []
    for item in items:
        created.append(_one(store, provider, kind, item, shop_id=shop_id))
    return created


def decide_draft(
    store: WorkflowStore,
    draft_id: str,
    action: str,
    note: str = "",
    *,
    decided_by: str = "console",
) -> dict:
    from shopkeeper.workflow.approvals import (
        ApprovalError,
        apply_approval_event,
        ensure_listing_approval,
        sync_draft_projection,
    )

    draft = store.get_draft(draft_id)
    if draft is None:
        raise DraftError("草稿不存在。")
    actor = (decided_by or "console").strip() or "console"
    if action == "approve":
        if draft["status"] != "pending":
            raise DraftError("只有待审且质检通过的草稿可以通过。")
        qa = draft.get("qa") or {}
        if not qa.get("ok"):
            block_hits = (qa.get("compliance") or {}).get("block_hits") or [
                h for h in (qa.get("hits") or []) if isinstance(h, dict) and h.get("severity") == "block"
            ]
            if block_hits:
                detail = "; ".join(
                    f"{h.get('term')}@{h.get('start')}-{h.get('end')}" for h in block_hits[:8]
                )
                raise DraftError(f"合规词 block 命中，不能通过审批：{detail}")
            raise DraftError("质检未通过，不能通过。")
        # 防御：即便 ok 被篡改，block 命中仍不可进 approved
        block_hits = (qa.get("compliance") or {}).get("block_hits") or [
            h for h in (qa.get("hits") or []) if isinstance(h, dict) and h.get("severity") == "block"
        ]
        if block_hits:
            detail = "; ".join(
                f"{h.get('term')}@{h.get('start')}-{h.get('end')}" for h in block_hits[:8]
            )
            raise DraftError(f"合规词 block 命中，不能通过审批：{detail}")
        if draft.get("kind") == "listing":
            approval = ensure_listing_approval(store, draft)
            try:
                apply_approval_event(
                    store,
                    approval["id"],
                    ApprovalEvent.APPROVE,
                    decided_by=actor,
                    note=note,
                )
            except ApprovalError as exc:
                raise DraftError(str(exc)) from exc
            saved = sync_draft_projection(store, draft_id, "approved", note)
        else:
            saved = store.decide(draft_id, "approved", note, allowed_from=("pending",))
    elif action == "reject":
        if draft["status"] in {"approved", "rejected"}:
            raise DraftError("这份草稿已经有决定。")
        if draft.get("kind") == "listing":
            approval = store.get_approval_for_entity(
                "draft",
                draft_id,
                action_type="create_listing",
                statuses=("pending",),
            )
            if approval is None:
                approval = ensure_listing_approval(store, draft)
            try:
                apply_approval_event(
                    store,
                    approval["id"],
                    ApprovalEvent.REJECT,
                    decided_by=actor,
                    note=note,
                )
            except ApprovalError as exc:
                raise DraftError(str(exc)) from exc
            saved = sync_draft_projection(store, draft_id, "rejected", note)
        else:
            saved = store.decide(draft_id, "rejected", note, allowed_from=("pending", "needs_revision"))
    else:
        raise DraftError("action 只能是 approve 或 reject。")
    if saved is None:
        current = store.get_draft(draft_id)
        if current is None:
            raise DraftError("草稿不存在。")
        raise DraftError("草稿状态已变，不能重复决定。")
    saved["published"] = bool(saved.get("published"))
    saved["sent"] = False
    return saved


def _redact_outbound_item(item: dict[str, str]) -> dict[str, str]:
    """只抹 message / specs 里的片段。商品 name 不按字段名整段丢掉。"""
    safe = dict(item)
    for key in ("message", "specs"):
        value = safe.get(key)
        if isinstance(value, str):
            safe[key], _hit = redact_spans(value)
    return safe


def _record_provider_usage(store: WorkflowStore, provider: ChatProvider, *, kind: str) -> None:
    """把一次真实 LLM 调用的用量落到 `orch_llm_usage`。

    ## 为什么要这么做

    `providers.py` 把用量累计在**内存**的 `provider.usage` dict 里，而**没有任何地方读它** ——
    进程一重启就没了，后台也看不到。两条 LLM 路径（编排器 / 本模块）必须写同一张表，
    `_llm_budget_health` 的数字才有意义。

    ## 边界

    - 只在**真实** provider 上记账：mock / 模板不产生费用，记进去会污染口径
    - provider 没有 `usage` / `cost_usd()` 时**安静跳过**（接口上没有这些的 provider
      本就不产生费用；不能因此让生成失败）
    - `run_id` 留空串：这条路径不隶属任何编排 run，但表结构要求非空
    """
    usage = getattr(provider, "usage", None)
    cost_fn = getattr(provider, "cost_usd", None)
    if not isinstance(usage, dict) or cost_fn is None:
        return
    try:
        cost = cost_fn()
    except Exception:  # noqa: BLE001 - 拿不到价目不阻断生成
        return
    if cost is None or float(cost) <= 0:
        return  # 没有价目（None）或零成本：不写 0 冒充已计量
    try:
        from shopkeeper.orchestrator.store import OrchestratorStore

        OrchestratorStore(store).record_llm_usage(
            run_id="",
            step_id=f"drafts:{kind}",
            model=str(getattr(provider, "last_model", None) or getattr(provider, "model", "") or ""),
            prompt_version="workflow-drafts",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(cost),
            rationale="workflow /drafts/generate（非编排器路径）",
        )
    except Exception:  # noqa: BLE001 - 记账失败不阻断生成
        return


def _one(
    store: WorkflowStore,
    provider: ChatProvider,
    kind: str,
    item: dict[str, str],
    *,
    shop_id: str | None = None,
) -> dict:
    source = provider.name
    degraded = provider.degraded
    model_error = ""
    if provider.name == "mock" or provider.degraded:
        content = render_mock(kind, item)
        source = "mock"
    else:
        safe_item = _redact_outbound_item(item)
        user = json.dumps({"kind": kind, "item": safe_item}, ensure_ascii=False)
        result = provider.complete(draft_system(), user)
        # 记账：与编排器写**同一张 `orch_llm_usage`**，口径才一致
        # （否则后台「今日剩余额度」看不到这条路径的消耗）。
        _record_provider_usage(store, provider, kind=kind)
        parsed = parse_model_json(result.data) if result.ok and result.data else None
        if not result.ok or parsed is None:
            from shopkeeper.adapters.llm_failover import is_model_failover_error

            if result.error and is_model_failover_error(result.error):
                # 主备超时/5xx/限流耗尽 → 本地模板 + degraded
                content = render_mock(kind, item)
                source = "mock"
                degraded = True
                model_error = f"LLM 主备失败已回落模板：{result.error.message}"
            else:
                content = {"title_th": "", "title_en": "", "body_th": "", "body_en": ""}
                model_error = result.error.message if result.error else "模型没有返回 JSON。"
        else:
            content = {
                "title_th": str(parsed.get("title_th") or ""),
                "title_en": str(parsed.get("title_en") or ""),
                "body_th": str(parsed.get("body_th") or ""),
                "body_en": str(parsed.get("body_en") or ""),
            }
    extra = item.get("message") or item.get("specs") or ""
    qa = review_copy(content["title_th"], content["body_th"], require_thai=True, extra=extra)
    if model_error:
        qa = {"ok": False, "issues": [model_error, *qa["issues"]], "escalate": qa.get("escalate", False)}
    status = "pending" if qa["ok"] else "needs_revision"
    title = content["title_th"] or content["title_en"] or item.get("name") or kind
    draft = store.add_draft(
        shop_id=shop_id,
        kind=kind,
        status=status,
        title=title,
        body_th=content["body_th"],
        body_en=content["body_en"],
        payload={
            "input": item,
            "title_en": content["title_en"],
            "provider_reason": provider.reason,
            "note": "通过只表示草稿可留用。上架须再点「请求上架」且仅 listing 会走适配器；发货与退款不会因此发生。",
        },
        qa=qa,
        source=source,
        degraded=degraded,
    )
    if kind == "listing" and status == "pending":
        from shopkeeper.workflow.approvals import ensure_listing_approval

        approval = ensure_listing_approval(store, draft)
        payload = dict(draft.get("payload") or {})
        payload["approval_id"] = approval["id"]
        return store.update_draft_payload(draft["id"], payload) or draft
    return draft
