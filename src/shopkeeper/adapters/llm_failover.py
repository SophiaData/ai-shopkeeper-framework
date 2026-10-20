"""LLM 主备模型切换与本地模板降级。

主模型遇超时 / 5xx / 限流（及传输层不可达）时，按 env 切换备用模型；
链上全部失败则回落本地模板，并在结果上标记 degraded。
备用调用与主调用一样计入 LLM_DAILY_BUDGET_USD（由调用方记账）。
"""

from __future__ import annotations

import os
import re
from typing import Any

from shopkeeper.errors import AdapterError
from shopkeeper.models import (
    LangVersion,
    ListingPackage,
    LlmListingOutcome,
    LlmUsage,
    ProductFacts,
)

CODE_LLM_FALLBACK_EXHAUSTED = "LLM_FALLBACK_EXHAUSTED"

_HTTP_FAILOVER = re.compile(r"^LLM_HTTP_([45]\d{2})$")
#: 触发切备用模型的码（超时 / 5xx / 限流 / 传输层）
_FAILOVER_EXACT = frozenset({
    "LLM_TIMEOUT",
    "LLM_TRANSPORT",
    "LLM_HTTP_429",
    "LLM_HTTP_500",
    "LLM_HTTP_502",
    "LLM_HTTP_503",
    "LLM_HTTP_504",
    "LLM_UPSTREAM_503",
    "LLM_HTTP_ERROR",  # 工作台 OpenAI 兼容通道的笼统码
})


def model_chain(
    env: dict[str, str] | None = None,
    *,
    primary: str | None = None,
) -> list[str]:
    """主模型 + LLM_FALLBACK_MODEL + LLM_FALLBACK_MODELS（逗号分隔），去重保序。"""
    source = env if env is not None else os.environ
    head = (primary if primary is not None else str(source.get("SHOPKEEPER_LLM_MODEL") or "")).strip()
    single = str(source.get("LLM_FALLBACK_MODEL") or "").strip()
    extras = str(source.get("LLM_FALLBACK_MODELS") or "").strip()
    ordered: list[str] = []
    for item in (head, single, *[p.strip() for p in extras.split(",") if p.strip()]):
        if item and item not in ordered:
            ordered.append(item)
    return ordered


def fallback_base_url(env: dict[str, str] | None = None, *, default: str = "") -> str:
    """备用模型专用 base_url；留空则复用主通道。"""
    source = env if env is not None else os.environ
    raw = str(source.get("LLM_FALLBACK_BASE_URL") or "").strip()
    return raw.rstrip("/") if raw else default.rstrip("/")


def is_model_failover_error(err: AdapterError | None) -> bool:
    """是否应切换备用模型（超时 / 5xx / 限流 / 传输失败）。"""
    if err is None:
        return False
    code = err.code or ""
    if code in _FAILOVER_EXACT:
        return True
    m = _HTTP_FAILOVER.match(code)
    if not m:
        return False
    status = int(m.group(1))
    return status == 429 or status >= 500


def template_listing_outcome(
    product: ProductFacts,
    langs: list[str],
    site: str = "SEA",
    *,
    failover_errors: list[dict[str, Any]] | None = None,
) -> LlmListingOutcome:
    """全部模型失败后的本地模板（不冒充模型输出）。"""
    versions: list[LangVersion] = []
    title_base = (product.title_raw or "product").strip() or "product"
    for lang in langs:
        code = lang.upper()
        versions.append(
            LangVersion(
                lang=code,
                title=f"[{code}] {title_base}"[:200],
                bullets=[
                    f"[{code}] Seller-provided facts only",
                    f"[{code}] Local template — not model-written",
                    f"[{code}] Human review required before publish",
                ],
                description=(
                    f"[{code}] Local template fallback after LLM failover exhausted. "
                    f"Facts from seller/source only: {title_base}."
                ),
                advisory_flags=["llm_degraded_template"],
            )
        )
    report = {
        "engine": "local-template",
        "degraded": True,
        "degrade_code": CODE_LLM_FALLBACK_EXHAUSTED,
        "failover_errors": list(failover_errors or []),
    }
    pkg = ListingPackage(
        product_id=product.source_item_id,
        site=site,
        langs=[v.lang for v in versions],
        versions=versions,
        compliance_report=report,
    )
    return LlmListingOutcome(
        package=pkg,
        usage=LlmUsage(prompt_tokens=0, completion_tokens=0, latency_ms=0, cost_amount=0.0, cost_currency="USD"),
        attempts=1,
        degraded=True,
        degrade_code=CODE_LLM_FALLBACK_EXHAUSTED,
    )


def template_orchestrator_payload(schema_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """编排器结构化调用耗尽后的本地模板载荷。"""
    base = {
        "rationale": "local template after LLM failover exhausted; not model-written",
        "ok": True,
        "recommend": True,
        "risk_notes": ["llm_degraded_template"],
        "rewrite": False,
        "issues": ["llm_degraded_template"],
    }
    if schema_fields:
        base.update(schema_fields)
    return base
