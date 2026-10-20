"""流水线各步骤处理器。涉钱写仍走八态闸门；发货/退款/付款不开。

幂等约定（断点续跑 / ``recover_stale_running_steps`` 后重入）：
- ``kind=execution``（publish / dropship / tracking）：若 context 已有成功副作用则短路径成功，禁止重复外呼。
- ``kind=deterministic`` 中的 order_sync：按 run_id 派生稳定 order_id + upsert。
- ``kind=ai``：允许再调 LLM（预算仍计）；副作用以 context / 草稿表为准。
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from shopkeeper.adapters.base import FulfillmentAdapter, PlatformAdapter, ProductSource
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.domain.scout_report import ASSUMED_RETAIL_MULTIPLIER, build_report, price_gap
from shopkeeper.scout.category_rules import listing_category_fit
from shopkeeper.errors import AdapterError
from shopkeeper.models import ProductFacts
from shopkeeper.site_profile import SiteProfile, profile_currency
from shopkeeper.orchestrator.llm import OrchestratorLLM, is_budget_error
from shopkeeper.orchestrator.outreach import (
    DRAFT_KIND,
    PROMPT_FILE as OUTREACH_PROMPT_FILE,
    PROMPT_VERSION as OUTREACH_PROMPT_VERSION,
    OutreachDecision,
    OutreachInputError,
    build_deterministic_outreach,
    listing_outreach_source,
    load_system_prompt,
    review_message,
    shape_problems,
    ungrounded_claims as outreach_ungrounded,
)
from shopkeeper.orchestrator.scripting import (
    PROMPT_FILE,
    PROMPT_VERSION as SCRIPT_PROMPT_VERSION,
    ScriptDecision,
    ScriptInputError,
    build_deterministic_script,
    listing_script_source,
    script_system,
    ungrounded_claims,
)
from shopkeeper.orchestrator.store import (
    STEP_AWAITING_APPROVAL,
    STEP_FAILED,
    STEP_SUCCEEDED,
    STEP_SUSPENDED,
    OrchestratorStore,
)
from shopkeeper.workflow.approvals import ensure_listing_approval
from shopkeeper.workflow.drafts import decide_draft
from shopkeeper.workflow.gate import publish_listing
from shopkeeper.workflow.shops import normalize_shop_id
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.thai_qa import review_copy


#: 订单来自工作台真实同步缓存（而不是演示 ``order_sync`` 生成的合成订单）。
ORDER_SOURCE_SYNCED_CACHE = "synced_cache"

#: 演示订单标记：``run_order_sync`` 写 payload.source=orchestrator_stub、
#: synced_orders.source=orchestrator。任一命中都不得进入真实履约 flow。
_SYNTHETIC_ORDER_MARKERS = frozenset({"orchestrator_stub", "orchestrator"})
_SYNTHETIC_ORDER_PREFIX = "ORD-ORCH-"


def is_synthetic_synced_order(order: dict[str, Any] | None) -> bool:
    """判断缓存订单是否由演示 ``order_sync`` 步骤合成。

    真实同步订单由 ``gate.py`` 以平台名（如 ``mock_platform``）写入 ``_source``，
    payload 通常没有 ``source``；演示订单则同时带 payload.source=orchestrator_stub
    和行级 ``_source=orchestrator``。这里再加 ``ORD-ORCH-`` 前缀作为防御性兜底。
    """
    if not isinstance(order, dict):
        return False
    payload_source = str(order.get("source") or "").strip().lower()
    row_source = str(order.get("_source") or "").strip().lower()
    order_id = str(order.get("order_id") or order.get("id") or "").strip()
    return (
        payload_source in _SYNTHETIC_ORDER_MARKERS
        or row_source in _SYNTHETIC_ORDER_MARKERS
        or order_id.startswith(_SYNTHETIC_ORDER_PREFIX)
    )


class StepResult:
    __slots__ = ("status", "output", "ai_trace", "error", "context_patch")

    def __init__(
        self,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        ai_trace: dict[str, Any] | list[Any] | None = None,
        error: dict[str, Any] | None = None,
        context_patch: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.output = output or {}
        self.ai_trace = ai_trace or {}
        self.error = error
        self.context_patch = context_patch or {}


class ScoutDecision(BaseModel):
    rationale: str = ""
    recommend: bool = True
    risk_notes: list[str] = Field(default_factory=list)


class PricingDecision(BaseModel):
    """LLM 只写定价理由；可选提出覆盖价（THB satang）。缺省=沿用公式价。"""

    rationale: str = ""
    proposed_price_thb: int | None = None


class StylistDecision(BaseModel):
    rationale: str = ""
    title_th: str
    title_en: str
    body_th: str
    body_en: str


class StepContext:
    def __init__(
        self,
        *,
        store: OrchestratorStore,
        workflow: WorkflowStore,
        llm: OrchestratorLLM,
        product_source: ProductSource | None = None,
        platform: PlatformAdapter | None = None,
        fulfillment: FulfillmentAdapter | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.store = store
        self.workflow = workflow
        self.llm = llm
        self.product_source = product_source or MockProductSource()
        self.platform = platform
        #: 真实履约 flow 的代发适配器；None → handler 内回落 MockAlibabaDropship（明确标记为 mock）。
        self.fulfillment = fulfillment
        self.env = env or {}


def run_scout(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    url = str((run.get("input") or {}).get("url") or (run.get("context") or {}).get("url") or "")
    target = (run.get("input") or {}).get("target_price_cents")
    if not url:
        return StepResult(
            STEP_FAILED,
            error={"code": "ORCH_MISSING_URL", "message": "启动 run 须提供 1688 url"},
        )
    result = ctx.product_source.fetch(url)
    if not result.ok or result.data is None:
        err = result.error or AdapterError.validation("P1688_FETCH_FAILED", "采集失败")
        return StepResult(STEP_FAILED, error={"code": err.code, "message": err.message, "kind": err.kind.value})

    facts = result.data
    report = build_report(facts, site="TH", target_price_cents=target if isinstance(target, int) else None)
    # 目标价（分）只参与差价。泰铢价带只看另给的 retail_price_thb，不把人民币成本换算过去。
    report["category_fit"] = listing_category_fit(
        text=facts.title_raw,
        retail_price_thb=(run.get("input") or {}).get("retail_price_thb"),
    )
    llm_res = ctx.llm.complete_json(
        run_id=run["id"],
        step_id="scout",
        system="You are Scout. Return JSON with recommend, risk_notes, rationale.",
        user=f"title={facts.title_raw} price_cents={facts.price_cents}",
        schema=ScoutDecision,
        mock_payload={
            "rationale": f"mock scout accept {facts.source_item_id}",
            "recommend": True,
            "risk_notes": [],
        },
    )
    if not llm_res.ok or llm_res.data is None:
        err = llm_res.error
        if is_budget_error(err):
            return StepResult(
                STEP_SUSPENDED,
                error={"code": "LLM_BUDGET_EXCEEDED", "message": err.message if err else "budget"},
                context_patch={"budget_paused": True},
            )
        return StepResult(
            STEP_FAILED,
            error={"code": err.code if err else "LLM_NO_RESULT", "message": err.message if err else "llm failed"},
        )
    decision = llm_res.data
    facts_dict = facts.model_dump()
    return StepResult(
        STEP_SUCCEEDED,
        output={
            "facts": facts_dict,
            "report": report,
            "recommend": decision.data.get("recommend", True),
        },
        ai_trace={
            "prompt_version": decision.prompt_version,
            "model": decision.model,
            "prompt_tokens": decision.prompt_tokens,
            "completion_tokens": decision.completion_tokens,
            "cost_usd": decision.cost_usd,
            "rationale": decision.rationale,
            "attempts": decision.attempts,
            "degraded": decision.degraded,
            "degrade_code": decision.degrade_code,
        },
        context_patch={"facts": facts_dict, "report": report, "url": url},
    )


def pricing_system_prompt(profile: SiteProfile | None = None) -> str:
    """定价步系统提示。币种名读画像，默认 THB。不改公式，不换汇。"""
    currency = profile_currency(profile)
    return (
        "You are Pricing aide. Prefer the formula suggested_price_thb. "
        f"Return JSON with rationale and optional proposed_price_thb ({currency} satang). "
        "If you omit proposed_price_thb, the formula price is used."
    )


def run_pricing(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """公式优先；LLM 写理由并校验偏离；超阈值挂起审批。无成本明确失败。"""
    from datetime import datetime, timezone

    from shopkeeper.pricing import (
        PricingError,
        QuoteInput,
        check_deviation,
        load_pricing_config,
        quote_price,
    )
    from shopkeeper.workflow.approvals import DECIDE_TTL, evaluate_gate_policy, payload_hash

    facts_raw = (run.get("context") or {}).get("facts") or {}
    if not facts_raw:
        return StepResult(STEP_FAILED, error={"code": "ORCH_NO_FACTS", "message": "pricing 缺少 scout facts"})
    facts = ProductFacts(**facts_raw)
    config = load_pricing_config(ctx.env)
    run_input = run.get("input") or {}

    # 已挂起过：检查审批是否通过后继续
    existing_approval_id = (run.get("context") or {}).get("pricing_approval_id")
    if existing_approval_id:
        approval = ctx.workflow.get_approval(existing_approval_id)
        prev = (run.get("context") or {}).get("pricing") or {}
        if approval and approval["status"] == "approved":
            # 批准后采用审批载荷中的最终价（通常为 LLM 覆盖价）
            payload = approval.get("payload") or {}
            final_price = int(payload.get("proposed_price_thb") or prev.get("suggested_price_thb") or 0)
            pricing = {
                **prev,
                "suggested_price_thb": final_price,
                "suggested_retail_cents": final_price,
                "price_source": "approved_override",
                "pricing_approval_id": existing_approval_id,
            }
            return StepResult(
                STEP_SUCCEEDED,
                output=pricing,
                context_patch={"pricing": pricing},
            )
        if approval and approval["status"] == "rejected":
            return StepResult(
                STEP_FAILED,
                error={"code": "GATE_NOT_APPROVED", "message": "定价偏离审批已拒绝"},
                output={"pricing_approval_id": existing_approval_id},
            )
        return StepResult(
            STEP_AWAITING_APPROVAL,
            output={"pricing_approval_id": existing_approval_id, "status": approval["status"] if approval else "missing"},
        )

    shipping_override = run_input.get("shipping_cny")
    try:
        quote = quote_price(
            QuoteInput(
                cost_cny=facts.price_cents,
                shipping_cny=int(shipping_override) if shipping_override is not None else None,
            ),
            config,
        )
    except PricingError as exc:
        return StepResult(STEP_FAILED, error=exc.as_dict())

    # 兼容旧字段：suggested_retail_cents = 公式 THB satang（不再用 3x 假倍率当权威）
    legacy_gap = price_gap(facts, quote.suggested_price_thb)
    force_dev = bool(run_input.get("force_price_deviation"))
    mock_proposed = quote.suggested_price_thb
    if force_dev:
        # 故意偏离：抬到公式价的 2 倍，确保超默认 10% 阈值
        mock_proposed = max(quote.suggested_price_thb * 2, quote.suggested_price_thb + 1)

    llm_res = ctx.llm.complete_json(
        run_id=run["id"],
        step_id="pricing",
        system=pricing_system_prompt(),
        user=(
            f"formula_suggested_price_thb={quote.suggested_price_thb}\n"
            f"cost_cny={facts.price_cents}\n"
            f"fx_rate={quote.fx_rate}\n"
            f"fx_source={quote.fx_source}\n"
            f"breakdown={quote.breakdown}\n"
            f"deviation_threshold_pct={config.deviation_pct}"
        ),
        schema=PricingDecision,
        mock_payload={
            "rationale": (
                f"mock pricing: use formula {quote.suggested_price_thb} satang"
                + (" (forced deviation)" if force_dev else "")
            ),
            "proposed_price_thb": mock_proposed,
        },
    )
    if not llm_res.ok or llm_res.data is None:
        err = llm_res.error
        if is_budget_error(err):
            return StepResult(
                STEP_SUSPENDED,
                error={"code": "LLM_BUDGET_EXCEEDED", "message": err.message if err else "budget"},
                context_patch={"budget_paused": True},
            )
        return StepResult(
            STEP_FAILED,
            error={"code": err.code if err else "LLM_NO_RESULT", "message": err.message if err else "llm failed"},
        )

    decision = llm_res.data
    proposed = decision.data.get("proposed_price_thb")
    if proposed is None:
        proposed = quote.suggested_price_thb
    else:
        proposed = int(proposed)

    deviation = check_deviation(
        formula_price_thb=quote.suggested_price_thb,
        proposed_price_thb=proposed,
        threshold_pct=config.deviation_pct,
    )

    pricing_base = {
        "cost_cents": facts.price_cents,
        "cost_cny": facts.price_cents,
        "currency": facts.currency,
        "suggested_price_thb": quote.suggested_price_thb,
        "suggested_retail_cents": quote.suggested_price_thb,
        "breakdown": quote.breakdown,
        "fx_source": quote.fx_source,
        "fx_rate": quote.fx_rate,
        "proposed_price_thb": proposed,
        "deviation": deviation.as_dict(),
        "price_gap": legacy_gap,
        "multiplier_assumption": ASSUMED_RETAIL_MULTIPLIER,
        "note": "公式价优先（占位费率）；净利由 Ledger 在订单级核算。ASSUMED_RETAIL_MULTIPLIER 仅作历史对照。",
        "config": config.as_public_dict(),
    }
    ai_trace = {
        "prompt_version": decision.prompt_version,
        "model": decision.model,
        "prompt_tokens": decision.prompt_tokens,
        "completion_tokens": decision.completion_tokens,
        "cost_usd": decision.cost_usd,
        "rationale": decision.rationale,
        "attempts": decision.attempts,
        "degraded": decision.degraded,
        "degrade_code": decision.degrade_code,
    }

    if deviation.within_threshold:
        # 阈值内：优先公式价（即便 LLM 提了小幅覆盖也回落公式，除非完全相等）
        final = quote.suggested_price_thb
        pricing = {
            **pricing_base,
            "suggested_price_thb": final,
            "suggested_retail_cents": final,
            "price_source": "formula",
        }
        return StepResult(
            STEP_SUCCEEDED,
            output=pricing,
            ai_trace=ai_trace,
            context_patch={"pricing": pricing},
        )

    # 偏离超阈值 → 先问策略表，再决定要不要建单
    policy = ctx.workflow.get_gate_policy("pricing_override")
    gate = evaluate_gate_policy(
        policy,
        action_type="pricing_override",
        context={"price_deviation_pct": deviation.deviation_pct},
    )

    # ⚠️ **必须真的按裁决走**，不能「算完只写进 decision_note」。
    #
    # 原实现无条件 `add_approval(...)` 并挂起 —— 于是
    # `evaluate_gate_policy` 返回的 `require_approval: False` **毫无作用**：
    # 把策略阈值放宽到 100%（偏离多少都免审），流程**照样**停下来等人批。
    # 实测：策略裁决 `auto_allow` / `require_approval: False`，
    # 仍然建出 `pricing_override: pending` 单并把 run 挂起。
    #
    # DESIGN §165 写的是「闸门裁决是纯函数，**输入来自规则表**」——
    # 规则表改不动行为，这个函数就只是装饰。
    #
    # 边界：`require_approval` 为真（或策略缺失 → 默认必审）时行为不变，
    # 仍然建单挂起，**不会**因此放宽任何涉钱动作。
    if not gate.get("require_approval", True):
        waived = {
            **pricing_base,
            "price_source": "policy_auto_allow",
            "gate_decision": gate.get("decision"),
            "gate_reason": gate.get("reason"),
        }
        ctx.workflow.add_gate_decision(
            approval_id=None,  # 免审：没有审批单，但要留痕
            policy_id=str((policy or {}).get("id") or "missing"),
            policy_version=int((policy or {}).get("version") or 0),
            input_snapshot={
                "run_id": run["id"],
                "formula_price_thb": quote.suggested_price_thb,
                "proposed_price_thb": proposed,
                "deviation_pct": deviation.deviation_pct,
            },
            decision=str(gate.get("decision") or "auto_allow"),
            reason=str(gate.get("reason") or ""),
        )
        return StepResult(
            STEP_SUCCEEDED,
            output=waived,
            context_patch={"pricing": waived},
        )
    payload = {
        "run_id": run["id"],
        "formula_price_thb": quote.suggested_price_thb,
        "proposed_price_thb": proposed,
        "deviation_pct": deviation.deviation_pct,
        "threshold_pct": config.deviation_pct,
        "fx_rate": quote.fx_rate,
        "fx_source": quote.fx_source,
        "cost_cny": facts.price_cents,
    }
    now = datetime.now(timezone.utc)
    approval = ctx.workflow.add_approval(
        {
            "action_type": "pricing_override",
            "entity_type": "orch_run",
            "entity_id": run["id"],
            "payload": payload,
            "payload_hash": payload_hash(payload),
            "status": "pending",
            "channel": (policy or {}).get("channel") or "console",
            "policy_id": (policy or {}).get("id"),
            "decide_before": (now + DECIDE_TTL).isoformat(),
            "execute_within_seconds": int(DECIDE_TTL.total_seconds()),
            "decision_note": gate["reason"],
        }
    )
    ctx.workflow.add_gate_decision(
        approval_id=approval["id"],
        policy_id=str((policy or {}).get("id") or "missing"),
        policy_version=int((policy or {}).get("version") or 0),
        input_snapshot=payload,
        decision=gate["decision"],
        reason=gate["reason"],
    )
    pricing = {
        **pricing_base,
        "price_source": "pending_override_approval",
        "pricing_approval_id": approval["id"],
    }
    return StepResult(
        STEP_AWAITING_APPROVAL,
        output=pricing,
        ai_trace=ai_trace,
        error={
            "code": "PRICING_DEVIATION",
            "message": deviation.reason,
        },
        context_patch={"pricing": pricing, "pricing_approval_id": approval["id"]},
    )


def run_stylist(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    facts_raw = (run.get("context") or {}).get("facts") or {}
    if not facts_raw:
        return StepResult(STEP_FAILED, error={"code": "ORCH_NO_FACTS", "message": "stylist 缺少 facts"})
    facts = ProductFacts(**facts_raw)
    pricing = (run.get("context") or {}).get("pricing") or {}
    qa_feedback = (run.get("context") or {}).get("qa_feedback") or ""
    rewrite_round = int((run.get("context") or {}).get("rewrite_round") or 0)

    mock_title = facts.title_raw[:40] if facts.title_raw else "storage"
    mock_payload = {
        "rationale": f"mock stylist round={rewrite_round}" + (f" rewrite:{qa_feedback[:80]}" if qa_feedback else ""),
        "title_th": f"กล่องจัดเก็บ {mock_title}",
        "title_en": f"Home storage: {mock_title}",
        "body_th": f"รายละเอียดตามข้อเท็จจริง: {facts.title_raw}",
        "body_en": f"Seller facts only: {facts.title_raw}. Price hint cents={pricing.get('suggested_retail_cents')}.",
    }
    # 测试可注入「故意不合规」文案以触发 QA 打回
    force_bad = bool((run.get("input") or {}).get("force_qa_fail")) and rewrite_round == 0
    if force_bad:
        mock_payload["body_th"] = "รับประกันคืนเงิน 100% และ MOQ: 1 wholesale"
        mock_payload["body_en"] = "Guaranteed refund and MOQ: 1 wholesale cost price"

    llm_res = ctx.llm.complete_json(
        run_id=run["id"],
        step_id="stylist",
        system="You are Stylist. Return JSON title/body TH+EN and rationale. No MOQ/refund promises.",
        user=json_user(facts, qa_feedback),
        schema=StylistDecision,
        mock_payload=mock_payload,
    )
    if not llm_res.ok or llm_res.data is None:
        err = llm_res.error
        if is_budget_error(err):
            return StepResult(
                STEP_SUSPENDED,
                error={"code": "LLM_BUDGET_EXCEEDED", "message": err.message if err else "budget"},
                context_patch={"budget_paused": True},
            )
        return StepResult(
            STEP_FAILED,
            error={"code": err.code if err else "LLM_NO_RESULT", "message": err.message if err else "llm failed"},
        )

    decision = llm_res.data
    copy = decision.data
    # 同步一份到 MockLLM listing 形状，便于后续扩展
    package = {
        "title_th": copy["title_th"],
        "title_en": copy["title_en"],
        "body_th": copy["body_th"],
        "body_en": copy["body_en"],
    }
    return StepResult(
        STEP_SUCCEEDED,
        output={"copy": package, "rewrite_round": rewrite_round},
        ai_trace={
            "prompt_version": decision.prompt_version,
            "model": decision.model,
            "prompt_tokens": decision.prompt_tokens,
            "completion_tokens": decision.completion_tokens,
            "cost_usd": decision.cost_usd,
            "rationale": decision.rationale,
            "attempts": decision.attempts,
            "qa_feedback": qa_feedback,
            "degraded": decision.degraded,
            "degrade_code": decision.degrade_code,
        },
        context_patch={"listing_copy": package, "qa_feedback": ""},
    )


def run_qa(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    copy = (run.get("context") or {}).get("listing_copy") or {}
    title_th = str(copy.get("title_th") or "")
    body_th = str(copy.get("body_th") or "")
    extra = f"{copy.get('title_en') or ''}\n{copy.get('body_en') or ''}"
    qa = review_copy(title_th, body_th, require_thai=True, extra=extra)
    rewrite_round = int((run.get("context") or {}).get("rewrite_round") or 0)
    max_rounds = int((run.get("context") or {}).get("qa_max_rewrite_rounds") or 2)
    if qa.get("ok"):
        return StepResult(
            STEP_SUCCEEDED,
            output={"qa": qa, "passed": True},
            context_patch={"qa": qa, "needs_rewrite": False},
        )
    # 不通过：通知引擎回退 Stylist
    feedback = "; ".join(qa.get("issues") or [])
    if rewrite_round < max_rounds:
        return StepResult(
            STEP_SUCCEEDED,
            output={"qa": qa, "passed": False, "rewrite": True, "feedback": feedback},
            context_patch={
                "qa": qa,
                "needs_rewrite": True,
                "qa_feedback": feedback,
                "rewrite_round": rewrite_round + 1,
            },
        )
    return StepResult(
        STEP_FAILED,
        output={"qa": qa, "passed": False, "rewrite": False},
        error={"code": "ORCH_QA_EXHAUSTED", "message": f"QA 打回已达上限 {max_rounds}: {feedback}"},
        context_patch={"qa": qa, "needs_rewrite": False},
    )


def _llm_failure(llm_res: Any) -> StepResult:
    err = llm_res.error
    if is_budget_error(err):
        return StepResult(
            STEP_SUSPENDED,
            error={"code": "LLM_BUDGET_EXCEEDED", "message": err.message if err else "budget"},
            context_patch={"budget_paused": True},
        )
    return StepResult(
        STEP_FAILED,
        error={"code": err.code if err else "LLM_NO_RESULT", "message": err.message if err else "llm failed"},
    )


def _ai_trace(decision: Any, **extra: Any) -> dict[str, Any]:
    trace = {
        "prompt_version": decision.prompt_version,
        "model": decision.model,
        "prompt_tokens": decision.prompt_tokens,
        "completion_tokens": decision.completion_tokens,
        "cost_usd": decision.cost_usd,
        "rationale": decision.rationale,
        "attempts": decision.attempts,
        "degraded": decision.degraded,
        "degrade_code": decision.degrade_code,
    }
    trace.update(extra)
    return trace


def run_script(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """QA 通过后的短视频脚本。纯生成，不过审批闸门、不外呼 TikTok。"""
    context = run.get("context") or {}
    try:
        source = listing_script_source(
            listing_copy=context.get("listing_copy") or {},
            facts=context.get("facts") or {},
            run_input=run.get("input") or {},
        )
    except ScriptInputError as exc:
        return StepResult(STEP_FAILED, error={"code": exc.code, "message": exc.message})

    mock_payload = build_deterministic_script(source)
    if bool((run.get("input") or {}).get("force_script_ungrounded")):
        mock_payload["shots"][2]["voice_zh"] += " 防水 食品级 承重 50kg 全网最低"
        mock_payload["shoot_checks"]["avoids_absolute_claims"] = False

    llm_res = ctx.llm.complete_json(
        run_id=run["id"],
        step_id="script",
        system=script_system(),
        user=(
            f"name={source['name']}\n"
            f"selling_points={source['selling_points']}\n"
            f"duration={source['duration_s']}\n"
            f"cart={'是' if source['cart'] else '否'}\n"
            f"scene={source['scene']}\n"
            "只复述 selling_points。不要补充未给出的规格。"
        ),
        schema=ScriptDecision,
        mock_payload=mock_payload,
    )
    if not llm_res.ok or llm_res.data is None:
        return _llm_failure(llm_res)

    decision = llm_res.data
    script = decision.data
    hits = ungrounded_claims(script, str(source["source_text"]))
    trace = _ai_trace(
        decision,
        prompt_file=PROMPT_FILE,
        script_prompt_version=SCRIPT_PROMPT_VERSION,
        duration_s=source["duration_s"],
        cart=source["cart"],
    )
    if hits:
        return StepResult(
            STEP_FAILED,
            output={"script": script, "ungrounded": hits},
            ai_trace=trace,
            error={
                "code": "ORCH_SCRIPT_UNGROUNDED",
                "message": "脚本写出了卖家未提供的规格或绝对化说法：" + "、".join(hits),
                "hits": hits,
            },
        )
    return StepResult(
        STEP_SUCCEEDED,
        output={"script": script, "duration_s": script.get("duration_s"), "cart": script.get("cart")},
        ai_trace=trace,
        context_patch={"video_script": script},
    )


def _outreach_rows_for_run(ctx: StepContext, run_id: str, shop_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for draft in ctx.workflow.list_drafts(shop_id=shop_id):
        if draft.get("kind") != DRAFT_KIND:
            continue
        payload = draft.get("payload") or {}
        if payload.get("run_id") == run_id:
            rows.append(draft)
    return rows


def _persist_outreach_drafts(
    ctx: StepContext,
    *,
    run: dict[str, Any],
    source: dict[str, Any],
    messages: list[dict[str, Any]],
    qa_by_key: dict[str, dict[str, Any]],
    degraded: bool,
) -> list[dict[str, Any]]:
    """按 run + 达人键复用已有草稿，避免断点重入写出第二份。"""
    shop_id = str(run.get("shop_id") or "default")
    existing = {
        str((row.get("payload") or {}).get("creator_key") or ""): row
        for row in _outreach_rows_for_run(ctx, run["id"], shop_id)
    }
    saved: list[dict[str, Any]] = []
    product = str(source["product"])
    for message, creator in zip(messages, source["creators"], strict=True):
        key = f"{creator['creator_type']}\n{creator['creator_name']}"
        prior = existing.get(key)
        if prior is not None and (prior.get("qa") or {}).get("ok") and prior.get("status") in {
            "pending",
            "approved",
            "rejected",
        }:
            saved.append(prior)
            continue
        qa = qa_by_key[key]
        draft = ctx.workflow.add_draft(
            kind=DRAFT_KIND,
            status="pending",
            title=f"{creator['creator_type']} · {creator['creator_name']} · {product}",
            body_th=str(message.get("dm_th") or ""),
            body_en=str(message.get("dm_en") or ""),
            payload={
                "source": "orchestrator",
                "run_id": run["id"],
                "creator_key": key,
                "creator_type": creator["creator_type"],
                "creator_name": creator["creator_name"],
                "audience": creator["audience"],
                "offer": creator["offer"],
                "budget_band": creator["budget_band"],
                "product": product,
                "message": message,
                "sent": False,
                "auto_send": False,
            },
            qa=qa,
            source="orchestrator",
            degraded=1 if degraded else 0,
            shop_id=shop_id,
        )
        saved.append(draft)
    return saved


def run_outreach(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """QA 与短视频脚本通过后的达人建联草稿。只入库待审，不发送、不外呼。"""
    context = run.get("context") or {}
    run_input = run.get("input") or {}
    try:
        source = listing_outreach_source(
            listing_copy=context.get("listing_copy") or {},
            facts=context.get("facts") or {},
            run_input=run_input,
        )
    except OutreachInputError as exc:
        return StepResult(STEP_FAILED, error={"code": exc.code, "message": exc.message})

    shop_id = str(run.get("shop_id") or "default")
    force_ungrounded = bool(run_input.get("force_outreach_ungrounded"))
    force_compliance = bool(run_input.get("force_outreach_compliance"))
    if not force_ungrounded and not force_compliance:
        existing = _outreach_rows_for_run(ctx, run["id"], shop_id)
        by_key = {str((row.get("payload") or {}).get("creator_key") or ""): row for row in existing}
        wanted = [f"{row['creator_type']}\n{row['creator_name']}" for row in source["creators"]]
        if wanted and all(
            key in by_key
            and (by_key[key].get("qa") or {}).get("ok")
            and by_key[key].get("status") in {"pending", "approved", "rejected"}
            for key in wanted
        ):
            drafts = [by_key[key] for key in wanted]
            messages = [(row.get("payload") or {}).get("message") or {} for row in drafts]
            return StepResult(
                STEP_SUCCEEDED,
                output={
                    "draft_ids": [row["id"] for row in drafts],
                    "count": len(drafts),
                    "sent": False,
                    "auto_send": False,
                    "idempotent_replay": True,
                    "messages": messages,
                },
                ai_trace={"rationale": "复用已落库的达人建联草稿，不重复生成、不发送", "degraded": False},
                context_patch={
                    "outreach": {
                        "draft_ids": [row["id"] for row in drafts],
                        "messages": messages,
                        "sent": False,
                        "auto_send": False,
                    }
                },
            )

    system, prompt_source = load_system_prompt()
    mock_payload = build_deterministic_outreach(source)
    if force_ungrounded:
        mock_payload["messages"][0]["dm_zh"] += " 佣金 30% 保底播放量 赠品 已发送 粉丝 12000 免打孔"
    if force_compliance:
        mock_payload["messages"][0]["dm_en"] += " miracle cure"

    creator_lines = "\n".join(
        f"- type={row['creator_type']} name={row['creator_name']} audience={row['audience']} "
        f"offer={row['offer']} budget_band={row['budget_band']}"
        for row in source["creators"]
    )
    llm_res = ctx.llm.complete_json(
        run_id=run["id"],
        step_id="outreach",
        system=system,
        user=(
            f"product={source['product']}\n"
            f"selling_points={source['selling_points']}\n"
            f"{creator_lines}\n"
            "只复述 selling_points 和上面的达人输入。不要补充未给出的佣金、粉丝数或规格。"
            "条数和顺序必须与达人列表一致。"
        ),
        schema=OutreachDecision,
        mock_payload=mock_payload,
    )
    if not llm_res.ok or llm_res.data is None:
        return _llm_failure(llm_res)

    decision = llm_res.data
    bundle = decision.data
    problems = shape_problems(bundle, source["creators"])
    hits = outreach_ungrounded(bundle, str(source["source_text"]))
    qa_by_key: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    for message, creator in zip(bundle.get("messages") or [], source["creators"], strict=False):
        key = f"{creator['creator_type']}\n{creator['creator_name']}"
        qa = review_message(message if isinstance(message, dict) else {})
        qa_by_key[key] = qa
        if not qa.get("ok"):
            blocked.append({"creator_key": key, "issues": qa.get("issues") or [], "compliance": qa.get("compliance")})
    trace = _ai_trace(
        decision,
        prompt_file=OUTREACH_PROMPT_FILE,
        outreach_prompt_version=OUTREACH_PROMPT_VERSION,
        prompt_source=prompt_source,
        creator_count=len(source["creators"]),
    )
    if problems:
        return StepResult(
            STEP_FAILED,
            output={"messages": bundle.get("messages") or [], "problems": problems},
            ai_trace=trace,
            error={
                "code": "ORCH_OUTREACH_INCOMPLETE",
                "message": "达人建联草稿结构与输入不一致：" + "、".join(problems),
                "problems": problems,
            },
        )
    if hits:
        return StepResult(
            STEP_FAILED,
            output={"messages": bundle.get("messages") or [], "ungrounded": hits},
            ai_trace=trace,
            error={
                "code": "ORCH_OUTREACH_UNGROUNDED",
                "message": "建联草稿写出了输入里没有的说法：" + "、".join(hits),
                "hits": hits,
            },
        )
    if blocked:
        block_hits: list[dict[str, Any]] = []
        for row in blocked:
            compliance = row.get("compliance") or {}
            block_hits.extend(compliance.get("block_hits") or [])
        return StepResult(
            STEP_FAILED,
            output={"messages": bundle.get("messages") or [], "qa": blocked},
            ai_trace=trace,
            error={
                "code": "ORCH_OUTREACH_COMPLIANCE",
                "message": "达人建联草稿未过合规或泰语质检，不进入待审",
                "hits": block_hits,
                "issues": [issue for row in blocked for issue in (row.get("issues") or [])],
            },
        )

    drafts = _persist_outreach_drafts(
        ctx,
        run=run,
        source=source,
        messages=list(bundle.get("messages") or []),
        qa_by_key=qa_by_key,
        degraded=bool(decision.degraded),
    )
    messages = list(bundle.get("messages") or [])
    return StepResult(
        STEP_SUCCEEDED,
        output={
            "draft_ids": [row["id"] for row in drafts],
            "count": len(drafts),
            "sent": False,
            "auto_send": False,
            "messages": messages,
        },
        ai_trace=trace,
        context_patch={
            "outreach": {
                "draft_ids": [row["id"] for row in drafts],
                "messages": messages,
                "sent": False,
                "auto_send": False,
            }
        },
    )


def run_human_approval(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """建 listing 草稿 + create_listing 八态审批；未批则挂起。"""
    copy = (run.get("context") or {}).get("listing_copy") or {}
    facts = (run.get("context") or {}).get("facts") or {}
    pricing = (run.get("context") or {}).get("pricing") or {}
    existing_draft_id = (run.get("context") or {}).get("draft_id")
    existing_approval_id = (run.get("context") or {}).get("approval_id")

    # ⚠️ **缺失质检必须失败，不能默认通过**（早于下面的审批早退）。
    #
    # 原先是 `or {"ok": True, "issues": []}` —— 缺 `qa` 时**伪造一条「通过」的记录**
    # 并作为草稿的 `qa` 字段存库。下游（审批页、报告）看到的是「质检通过」，
    # 而实际上**根本没跑过质检**。
    #
    # **位置很关键**：必须放在 `existing_approval_id` 早退**之前**。
    # 我第一版放在后面，结果复现用例走的正是早退分支（已有审批单 → 直接
    # `STEP_AWAITING_APPROVAL`），校验根本没机会执行 —— 「修了但没生效」。
    #
    # 触发面不只是「手工改库」：`_next_ready_step` 按「第一个非 succeeded 的步」
    # 线性扫描，只要 `qa` 步不是 succeeded（例如被 `retry_step` 置回 pending）
    # 而后面的步依赖恰好都满足，`human_approval` 就会被选中执行。
    # 另外旧版本写下的 run 行本身就可能没有 `qa` 键。
    #
    # 默认通过在这里是**最坏的默认**：它把「没检查」记成「检查过且合格」。
    qa = (run.get("context") or {}).get("qa")
    if not isinstance(qa, dict) or "ok" not in qa:
        return StepResult(
            STEP_FAILED,
            error={
                "code": "ORCH_QA_MISSING",
                "message": "缺少质检结果，不能进入审批（缺失不等于通过）。",
            },
        )


    if existing_approval_id:
        approval = ctx.workflow.get_approval(existing_approval_id)
        if approval and approval["status"] == "approved":
            return StepResult(
                STEP_SUCCEEDED,
                output={"draft_id": existing_draft_id, "approval_id": existing_approval_id, "status": "approved"},
            )
        if approval and approval["status"] == "rejected":
            return StepResult(
                STEP_FAILED,
                error={"code": "GATE_NOT_APPROVED", "message": "审批已拒绝，流水线停止"},
                output={"draft_id": existing_draft_id, "approval_id": existing_approval_id, "status": "rejected"},
            )
        return StepResult(
            STEP_AWAITING_APPROVAL,
            output={"draft_id": existing_draft_id, "approval_id": existing_approval_id, "status": approval["status"] if approval else "missing"},
        )

    draft = ctx.workflow.add_draft(
        shop_id=run.get("shop_id"),
        kind="listing",
        status="pending" if qa.get("ok") else "needs_revision",
        title=str(copy.get("title_en") or facts.get("title_raw") or "orchestrator listing"),
        body_th=str(copy.get("body_th") or ""),
        body_en=str(copy.get("body_en") or ""),
        payload={
            "source": "orchestrator",
            "run_id": run["id"],
            "facts": facts,
            "pricing": pricing,
            "copy": copy,
            "video_script": (run.get("context") or {}).get("video_script") or {},
            "outreach": (run.get("context") or {}).get("outreach") or {},
        },
        qa=qa,
        source="orchestrator",
        degraded=0,
    )
    if not qa.get("ok"):
        # block 级合规命中优先发 COMPLIANCE_TERM_HIT（带位置）；其余质检失败用 ORCH_QA_BLOCK
        block_hits = (qa.get("compliance") or {}).get("block_hits") or [
            h for h in (qa.get("hits") or []) if h.get("severity") == "block"
        ]
        if block_hits:
            err = {
                "code": "COMPLIANCE_TERM_HIT",
                "message": "合规词 block 命中，文案不可进入审批通过",
                "hits": block_hits,
                "terms": [h.get("term") for h in block_hits if h.get("term")],
            }
        else:
            err = {
                "code": "ORCH_QA_BLOCK",
                "message": "质检未通过不能进审批",
                "issues": qa.get("issues") or [],
            }
        return StepResult(
            STEP_FAILED,
            error=err,
            output={"draft_id": draft["id"], "qa": qa},
        )
    approval = ensure_listing_approval(ctx.workflow, draft)
    return StepResult(
        STEP_AWAITING_APPROVAL,
        output={"draft_id": draft["id"], "approval_id": approval["id"], "status": approval["status"]},
        context_patch={"draft_id": draft["id"], "approval_id": approval["id"]},
    )


def run_publish(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """上架执行步。幂等：context 已有成功 publish_result 则不再调 PlatformAdapter。"""
    draft_id = (run.get("context") or {}).get("draft_id")
    if not draft_id:
        return StepResult(STEP_FAILED, error={"code": "ORCH_NO_DRAFT", "message": "publish 缺少 draft_id"})
    if ctx.platform is None:
        return StepResult(STEP_FAILED, error={"code": "ORCH_NO_PLATFORM", "message": "未注入 PlatformAdapter"})

    # 幂等短路径：断点续跑时若已成功上架，直接成功（不重复外呼）
    prev = (run.get("context") or {}).get("publish_result") or {}
    prev_pid = (run.get("context") or {}).get("platform_product_id") or prev.get("platform_product_id")
    if prev_pid and (prev.get("published") or prev.get("status") in {"executed", "published", "dry_run"}):
        return StepResult(
            STEP_SUCCEEDED,
            output={
                "draft_id": draft_id,
                "platform_product_id": prev_pid,
                "status": prev.get("status") or "executed",
                "published": bool(prev.get("published", True)),
                "dry_run": bool(prev.get("dry_run")),
                "idempotent_replay": True,
            },
            context_patch={
                "platform_product_id": prev_pid,
                "publish_result": prev if prev else {"platform_product_id": prev_pid, "published": True},
            },
        )

    # 确保草稿投影为 approved（审批通过后续跑）
    draft = ctx.workflow.get_draft(draft_id)
    if draft is None:
        return StepResult(STEP_FAILED, error={"code": "ORCH_NO_DRAFT", "message": "草稿不存在"})
    if draft["status"] == "pending":
        # ⚠️ **必须先确认真的有人批准过**，不能假设「草稿还是 pending 就一定是批准后 resume 进来的」。
        #
        # 原实现直接 `decide_draft(..., "approve")`，注释写的是
        # 「orchestrator resume after approval」—— 但**没有任何代码验证这件事**。
        # 而 `publish` 步只要被选中就会进这个分支，于是存在这条路径：
        #
        #   1. `retry_step`（公开端点 POST /runs/{id}/steps/{sid}/retry）把任意步置回 pending
        #   2. 依赖步恰好都是 succeeded → `_next_ready_step` 选中 `publish`
        #   3. `run_publish` 看到草稿是 pending，**自动批准并上架**
        #   4. 审批单**全程是 pending**，却被消费成 executed
        #
        # 实测（修复前）：审批单 `create_listing: pending` 的状态下，
        # 平台确实收到了 `create_listing` 调用，商品上架。
        #
        # **闸门不能只活在 DAG 的某一个节点里** —— 涉钱动作的守卫必须紧贴外呼本身。
        approved = ctx.workflow.get_approval_for_entity(
            "draft", draft_id, action_type="create_listing", statuses=("approved",)
        )
        if approved is None:
            return StepResult(
                STEP_AWAITING_APPROVAL,
                error={
                    "code": "GATE_NOT_APPROVED",
                    "message": "未找到已批准的 create_listing 审批单，拒绝上架。",
                },
            )
        try:
            decide_draft(ctx.workflow, draft_id, "approve", note="orchestrator resume after approval")
        except Exception as exc:  # noqa: BLE001
            return StepResult(STEP_FAILED, error={"code": "GATE_NOT_APPROVED", "message": str(exc)})

    # 可选：强制走 TTS_WRITE_BLOCKED 路径（测试 / tiktok_live 未放行）
    force_block = bool((run.get("input") or {}).get("force_tts_write_blocked"))
    if force_block:
        return StepResult(
            STEP_SUSPENDED,
            error={
                "code": "TTS_WRITE_BLOCKED",
                "message": "真写未放行：TTS_WRITE_ENABLED / 白名单未开。挂起等待人工或改 mock。",
            },
            output={"blocked": True},
            context_patch={"publish_blocked": True},
        )

    try:
        view = publish_listing(ctx.workflow, ctx.platform, draft_id)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "TTS_WRITE_BLOCKED" in msg or "真写" in msg or "未放行" in msg:
            return StepResult(
                STEP_SUSPENDED,
                error={"code": "TTS_WRITE_BLOCKED", "message": msg},
                context_patch={"publish_blocked": True},
            )
        return StepResult(STEP_FAILED, error={"code": "ORCH_PUBLISH_FAILED", "message": msg})

    # publish_listing 内部失败可能写 execute_failed
    if view.get("status") in {"execute_failed", "failed"}:
        err_msg = str(view.get("execution_error") or view.get("last_error") or "publish failed")
        if "TTS_WRITE_BLOCKED" in err_msg:
            return StepResult(
                STEP_SUSPENDED,
                error={"code": "TTS_WRITE_BLOCKED", "message": err_msg},
                output=view,
                context_patch={"publish_blocked": True},
            )
        return StepResult(STEP_FAILED, error={"code": "ORCH_PUBLISH_FAILED", "message": err_msg}, output=view)

    return StepResult(
        STEP_SUCCEEDED,
        output={
            "draft_id": draft_id,
            "platform_product_id": view.get("platform_product_id"),
            "status": view.get("status"),
            "published": bool(view.get("published")),
            "dry_run": bool(view.get("dry_run")),
        },
        context_patch={
            "platform_product_id": view.get("platform_product_id"),
            "publish_result": view,
        },
    )


def run_order_sync(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """上架后只读订单 stub：写入一条本地同步订单，不履约。

    幂等：order_id 由 run_id 稳定派生，``upsert_synced_order`` 可安全重入。
    """
    # ⚠️ **不要只取 8 位**（32 bit）。
    #
    # 原先是 `f"ORD-ORCH-{(run['id'])[:8]}"` —— 8 个十六进制字符 = **32 bit**，
    # 生日碰撞在约 **9.3k 个 run** 时就有 1% 概率撞车。而
    # `upsert_synced_order` 是 ON CONFLICT 覆盖，**两笔订单会静默合并成一条**：
    #
    #   run1 = "abcdef12aaaa0000" / run2 = "abcdef12bbbb1111"
    #   → 都派生出 ORD-ORCH-abcdef12
    #   → 落库只剩一条（后者覆盖前者），**无任何报错**
    #
    # `run['id']` 是 `uuid4().hex`（32 位十六进制），直接全用即可 ——
    # 既不额外占存储，也不用新造 id 方案。
    order_id = f"ORD-ORCH-{run['id']}"
    payload = {
        "order_id": order_id,
        "status": "AWAITING_SHIPMENT",
        "currency": profile_currency(),
        "total_amount": "199",
        "items": [{"sku": "ORCH-SKU", "qty": 1, "unit_cost_cents": 1200}],
        "source": "orchestrator_stub",
    }
    ctx.workflow.upsert_synced_order(payload, source="orchestrator", shop_id=run.get("shop_id"))
    return StepResult(
        STEP_SUCCEEDED,
        output={"order_id": order_id, "synced": True},
        context_patch={"order_id": order_id},
    )


def _fulfillment_state(
    fulfillment: FulfillmentAdapter, *, mode: str = "mock"
) -> dict[str, Any]:
    """记录本次代发用的适配器性质，避免把 mock 结果当成真实 1688 平台事实。"""
    name = str(getattr(fulfillment, "name", "fulfillment") or "fulfillment")
    manual_only = bool(getattr(fulfillment, "manual_only", False))
    is_mock = (
        bool(getattr(fulfillment, "is_mock", False))
        or name.lower().startswith("mock")
        or manual_only
    )
    real_write = bool(getattr(fulfillment, "real_external_write", False))
    if name.lower().startswith("mock"):
        note = "mock 代发适配器：未调用真实 1688、未下单、未付款。"
    elif manual_only:
        note = "人工代发通道：只生成可复制下单信息；本系统不调用 1688、不付款。"
    else:
        note = "代发适配器：当前契约不包含真实付款；执行结果须由适配器如实提供。"
    return {
        "fulfillment_mode": str(mode or "mock").strip().lower() or "mock",
        "fulfillment_adapter": name,
        "fulfillment_is_mock": is_mock,
        "fulfillment_real_external_write": real_write,
        "fulfillment_note": note,
    }


def run_order_load(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """只读载入已同步订单，并校验它来自真实缓存、属于本 run 的 shop。

    该步是 ``order_fulfillment_v1`` 的入口闸门：
    - 演示 ``order_sync`` 生成的 ``orchestrator_stub`` 订单不得流入真实履约；
    - 订单不存在 / 店铺不匹配直接失败，不继续建代发审批。
    """
    data = run.get("context") or {}
    order_id = str(
        data.get("order_id") or (run.get("input") or {}).get("order_id") or ""
    ).strip()
    if not order_id:
        return StepResult(
            STEP_FAILED,
            error={"code": "ORDER_ID_REQUIRED", "message": "订单履约流程缺少 order_id"},
        )
    order = ctx.workflow.get_synced_order(order_id)
    if order is None:
        return StepResult(
            STEP_FAILED,
            error={"code": "ORDER_NOT_FOUND", "message": f"已同步订单不存在：{order_id}"},
        )
    if is_synthetic_synced_order(order):
        return StepResult(
            STEP_FAILED,
            error={
                "code": "ORDER_DEMO_REJECTED",
                "message": "演示/合成订单不得进入真实履约流程（source=orchestrator_stub）",
            },
        )
    order_shop = normalize_shop_id(order.get("shop_id"))
    run_shop = normalize_shop_id(
        run.get("shop_id") or data.get("shop_id") or order_shop
    )
    if order_shop != run_shop:
        return StepResult(
            STEP_FAILED,
            error={
                "code": "ORDER_SHOP_MISMATCH",
                "message": f"订单属于店铺 {order_shop}，与 run 店铺 {run_shop} 不一致",
            },
        )
    return StepResult(
        STEP_SUCCEEDED,
        output={
            "order_id": order_id,
            "status": order.get("status"),
            "shop_id": order_shop,
            "synced_source": order.get("_source"),
        },
        context_patch={
            "order_id": order_id,
            "order_source": ORDER_SOURCE_SYNCED_CACHE,
            "shop_id": order_shop,
        },
    )


def run_dropship(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    """代发草稿/执行；当前只接 mock 或人工通道，不真实下单、不付款。

    ``order_fulfillment_v1`` 使用注入的 ``ctx.fulfillment``（测试可传 fake）；
    未注入时回落 ``MockAlibabaDropship``，并在 context 里明确记录适配器性质。
    """
    from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
    from shopkeeper.workflow.dropship import create_dropship_draft, execute_dropship

    order_id = (run.get("context") or {}).get("order_id")
    if not order_id:
        return StepResult(STEP_FAILED, error={"code": "DROPSHIP_ORDER_REQUIRED", "message": "缺少 order_id"})

    fulfillment = ctx.fulfillment or MockAlibabaDropship()
    # 只从 run context 读取显式模式；当前只实现 mock / 人工离线，不臆造真实端点。
    fulfillment_mode = str((run.get("context") or {}).get("fulfillment_mode") or "mock")
    fulfillment_state = _fulfillment_state(fulfillment, mode=fulfillment_mode)
    existing = (run.get("context") or {}).get("dropship_approval_id")
    if existing:
        approval = ctx.workflow.get_approval(existing)
        if approval and approval["status"] in {"approved", "executing"}:
            if approval["status"] == "executing":
                return StepResult(
                    STEP_SUSPENDED,
                    output={"approval_id": existing, "order_id": order_id, "approval_status": "executing", **fulfillment_state},
                    context_patch=fulfillment_state,
                )
            try:
                result = execute_dropship(ctx.workflow, fulfillment, order_id)
            except Exception as exc:  # noqa: BLE001
                return StepResult(STEP_FAILED, error={"code": "DROPSHIP_PLACE_FAILED", "message": str(exc)})
            out = result if isinstance(result, dict) else {"ok": True}
            draft = (result.get("draft") if isinstance(result, dict) else {}) or {}
            if str(draft.get("status") or "") == "awaiting_manual_po":
                return StepResult(
                    STEP_SUSPENDED,
                    output={
                        "approval_id": existing,
                        "order_id": order_id,
                        "manual_order": result.get("manual_order") or {},
                        "note": "等待人工到 1688 下单并回填采购单号；未完成前不进入物流。",
                        **fulfillment_state,
                    },
                    context_patch={
                        **fulfillment_state,
                        "manual_po_pending": True,
                        "manual_order": result.get("manual_order") or {},
                    },
                )
            ds_result = dict(
                draft.get("result")
                or (result.get("adapter") if isinstance(result, dict) else None)
                or (result.get("result") if isinstance(result, dict) else None)
                or {}
            )
            return StepResult(
                STEP_SUCCEEDED,
                output=out,
                context_patch={
                    **fulfillment_state,
                    "dropship_result": ds_result,
                    "dropship_result_source": (
                        "mock_adapter"
                        if fulfillment_state.get("fulfillment_is_mock")
                        and not fulfillment_state.get("fulfillment_real_external_write")
                        else "adapter_execute"
                    ),
                },
            )
        if approval and approval["status"] == "executed":
            draft = ctx.workflow.get_dropship_draft(order_id) or {}
            ds_result = dict(draft.get("result") or {})
            return StepResult(
                STEP_SUCCEEDED,
                output={"order_id": order_id, "approval_id": existing, "draft": draft, "adopted": True, **fulfillment_state},
                context_patch={
                    **fulfillment_state,
                    "dropship_result": ds_result,
                    "dropship_result_source": "adopted_executed",
                },
            )
        if approval and approval["status"] == "rejected":
            return StepResult(STEP_FAILED, error={"code": "GATE_NOT_APPROVED", "message": "代发审批已拒绝"})
        if approval and approval["status"] in {"execute_failed", "stale"}:
            return StepResult(
                STEP_FAILED,
                error={"code": "DROPSHIP_EXECUTE_FAILED", "message": f"代发审批状态 {approval['status']}，须人工处理"},
            )
        return StepResult(
            STEP_AWAITING_APPROVAL,
            output={"approval_id": existing, "order_id": order_id, **fulfillment_state},
            context_patch=fulfillment_state,
        )

    try:
        created = create_dropship_draft(ctx.workflow, fulfillment, order_id)
    except Exception as exc:  # noqa: BLE001
        return StepResult(STEP_FAILED, error={"code": "DROPSHIP_DRAFT_FAILED", "message": str(exc)})
    approval = created.get("approval") if isinstance(created, dict) else None
    approval_id = approval.get("id") if isinstance(approval, dict) else None
    return StepResult(
        STEP_AWAITING_APPROVAL,
        output={"order_id": order_id, "approval_id": approval_id, "draft": created, **fulfillment_state},
        context_patch={
            **fulfillment_state,
            "dropship_approval_id": approval_id,
            "dropship_draft": created,
        },
    )


def run_tracking(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    from shopkeeper.workflow.tracking import create_tracking_draft, push_tracking

    order_id = (run.get("context") or {}).get("order_id")
    if not order_id:
        return StepResult(STEP_FAILED, error={"code": "TRACKING_REQUIRED", "message": "缺少 order_id"})

    data = run.get("context") or {}
    existing = data.get("tracking_approval_id")
    if not existing:
        # 工作台可能已经人工补录并生成了物流草稿；先采纳当前草稿的审批，
        # 避免重新按代发结果（尤其 mock 结果）造一份新的。
        draft = ctx.workflow.get_tracking_draft(order_id)
        if draft is not None and draft.get("approval_id"):
            existing = draft["approval_id"]

    if existing:
        approval = ctx.workflow.get_approval(existing)
        if approval and approval["status"] == "approved":
            if ctx.platform is None:
                return StepResult(STEP_FAILED, error={"code": "ORCH_NO_PLATFORM", "message": "无平台适配器"})
            try:
                result = push_tracking(ctx.workflow, ctx.platform, order_id)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "TTS_WRITE_BLOCKED" in msg:
                    return StepResult(
                        STEP_SUSPENDED,
                        error={"code": "TTS_WRITE_BLOCKED", "message": msg},
                    )
                return StepResult(STEP_FAILED, error={"code": "TRACKING_PUSH_FAILED", "message": msg})
            return StepResult(STEP_SUCCEEDED, output=result if isinstance(result, dict) else {"ok": True})
        if approval and approval["status"] == "executed":
            return StepResult(
                STEP_SUCCEEDED,
                output={"approval_id": existing, "already_executed": True},
            )
        if approval and approval["status"] in {"rejected", "execute_failed", "stale", "expired"}:
            return StepResult(
                STEP_SUSPENDED,
                error={
                    "code": "TRACKING_HUMAN_REQUIRED",
                    "message": (
                        f"物流审批当前为 {approval['status']}，已挂起等待人工在工作台重新生成并审批物流草稿；"
                        "不会用旧批准或演示数据重试。"
                    ),
                },
                context_patch={"tracking_human_required": True},
            )
        return StepResult(STEP_AWAITING_APPROVAL, output={"approval_id": existing})

    # 从代发结果取运单。演示 PIPELINE_V1 保留确定性兜底；真实同步订单禁止伪造。
    ds = data.get("dropship_result") or {}
    carrier = str(ds.get("carrier") or "").strip()
    tracking_no = str(ds.get("tracking_no") or "").strip()
    synced_order = str(data.get("order_source") or "") == ORDER_SOURCE_SYNCED_CACHE
    # 真实同步订单只接受真实代发适配器的结果；mock/演示适配器即使返回了
    # JNT/JT 形状的字段，也不能当成平台物流事实。
    mock_dropship = data.get("dropship_result_source") == "mock_adapter" or (
        bool(data.get("fulfillment_is_mock"))
        and not bool(data.get("fulfillment_real_external_write"))
    )
    if synced_order and (mock_dropship or not carrier or not tracking_no):
        reason = (
            "代发结果来自 mock/演示适配器，不能当作真实物流信息；已挂起等待人工补录。"
            if mock_dropship
            else "代发结果缺少真实承运商或物流单号，已挂起等待人工补录；"
            "不得用 run id 派生或默认 JNT 运单冒充平台事实。"
        )
        return StepResult(
            STEP_SUSPENDED,
            error={"code": "TRACKING_HUMAN_REQUIRED", "message": reason},
            context_patch={"tracking_human_required": True},
        )
    if synced_order:
        context_patch = {"tracking_source": "dropship_result"}
    else:
        # 仅演示流程允许的确定性兜底：JNT 白名单格式（JT + 纯数字）。
        if not carrier:
            carrier = "JNT"
        if not tracking_no:
            digits = "".join(c for c in run["id"] if c.isdigit())[:12].ljust(12, "0")
            tracking_no = f"JT{digits}"
        context_patch = {"tracking_source": "demo_fallback"}
    try:
        created = create_tracking_draft(
            ctx.workflow,
            order_id,
            carrier=carrier,
            tracking_no=tracking_no,
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult(STEP_FAILED, error={"code": "TRACKING_REQUIRED", "message": str(exc)})
    approval = created.get("approval") if isinstance(created, dict) else None
    approval_id = approval.get("id") if isinstance(approval, dict) else None
    return StepResult(
        STEP_AWAITING_APPROVAL,
        output={"approval_id": approval_id, "draft": created},
        context_patch={
            "tracking_approval_id": approval_id,
            "tracking_carrier": carrier,
            "tracking_no": tracking_no,
            **context_patch,
        },
    )


def run_cs(ctx: StepContext, run: dict[str, Any], step: dict[str, Any]) -> StepResult:
    from shopkeeper.workflow.cs import create_cs_draft, ingest_inbound

    order_id = (run.get("context") or {}).get("order_id") or ""
    inbound = ingest_inbound(
        ctx.workflow,
        conversation_id=f"conv-{(run['id'])[:8]}",
        order_id=order_id or None,
        category="logistics_query",
        body="พัสดุอยู่ไหนครับ",
        lang="th",
    )
    # 白名单物流类：建草稿并 stub 发送（sent_stub / dry_run，不外呼 TikTok 客服）
    draft = create_cs_draft(
        ctx.workflow,
        message_id=inbound["id"] if isinstance(inbound, dict) else None,
        env=ctx.env,
        auto_send=True,
    )
    sent = bool(
        (isinstance(draft, dict) and (draft.get("send") or {}).get("sent_stub"))
        or (isinstance(draft, dict) and ((draft.get("draft") or {}).get("sent_stub")))
    )
    return StepResult(
        STEP_SUCCEEDED,
        output={
            "inbound": inbound,
            "draft": draft,
            "sent_stub": sent,
            "note": "白名单物流类已 stub 发送（sent_stub/dry_run，不外呼平台）"
            if sent
            else "白名单物流类草稿已建；发送结果见 draft.send",
        },
        context_patch={"cs_draft": draft},
    )


def json_user(facts: ProductFacts, qa_feedback: str) -> str:
    base = f"title={facts.title_raw}\nprice_cents={facts.price_cents}\ncurrency={facts.currency}"
    if qa_feedback:
        return base + f"\nQA feedback to fix: {qa_feedback}"
    return base


HANDLERS: dict[str, Callable[[StepContext, dict[str, Any], dict[str, Any]], StepResult]] = {
    "scout": run_scout,
    "pricing": run_pricing,
    "stylist": run_stylist,
    "qa": run_qa,
    "script": run_script,
    "outreach": run_outreach,
    "human_approval": run_human_approval,
    "publish": run_publish,
    "order_sync": run_order_sync,
    "order_load": run_order_load,
    "dropship": run_dropship,
    "tracking": run_tracking,
    "cs": run_cs,
}
