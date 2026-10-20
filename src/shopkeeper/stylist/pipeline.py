"""listing 师 Stylist：商品事实 → 三语言文案 + 商品图（DESIGN §3.2 第二行）。

产出**只是草稿**（不发店铺，DESIGN §9 M1-B「纯生成」）。

三条来自 M1-A 实测的硬约束，已内建：
1. 合规双轨（§7.4）：模型自查 flags 与词表命中**取并集**，词表是主轨——
   模型可能整句飘或截断，词表确定性可审计（A2 实测结论）。
2. 图像降级（A3 裁决）：图像失败不得谎报成功，落 `original_kept + needs_human`。
3. 语种来自配置（M1-PLAN §0）：缺省 EN,TH,VI，扩语种只改配置。

M2：注入 `RedrawAwareImageClient` 时，`IMAGE_REDRAW_ENABLED=1` 可选走真重绘；
重绘后适配器内必跑角标水印检测。产物仍由 demo_package 收进
`packages/listing_<id>/images/`。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from shopkeeper.adapters.base import ImageClient, LlmClient
from shopkeeper.compliance import hits_to_report, scan_listing_hits, scan_listing_leakage
from shopkeeper.config import Settings
from shopkeeper.db.repo import (
    VersionConflictError,
    add_listing_version,
    finish_task,
    get_or_create_listing,
    record_tool_call,
    submit_task,
)
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.models import LlmListingOutcome, ProductFacts
from shopkeeper.retry import call_generation
from shopkeeper.security.error_codes import resolve


class StylistPipeline:
    def __init__(
        self,
        session: Session,
        llm: LlmClient,
        image: ImageClient,
        settings: Settings,
        *,
        shop_id: str,
        max_images_per_lang: int = 1,
    ) -> None:
        self.session = session
        self.llm = llm
        self.image = image
        self.settings = settings
        self.shop_id = shop_id
        self.max_images_per_lang = max_images_per_lang

    # ------------------------------------------------------------------

    def run(self, facts: ProductFacts, *, product_db_id: str) -> dict[str, Any]:
        """生成三语言 listing 包 + 图片资产，落 listing_versions（version 自增）。"""
        langs = list(self.settings.langs)
        task, created = submit_task(
            self.session,
            agent="stylist",
            type="generate_listing",
            input={"product_id": product_db_id, "langs": langs, "site": self.settings.site},
            idempotency_key=f"listing:gen:{product_db_id}:{','.join(langs)}:{self.settings.site}",
            shop_id=self.shop_id,
        )
        self.session.commit()
        if not created:
            return {"task_id": task.id, "reused": True, "status": task.status, "output": task.output, "error": task.error}

        timings: dict[str, int] = {}
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        degraded: list[dict[str, Any]] = []

        # ---- 1. 文案（生成类：重试 2 次 → 人工待办） ----
        t0 = time.monotonic()
        llm_res, attempts = call_generation(lambda: self.llm.generate_listing(facts, langs, site=self.settings.site))
        timings["llm_ms"] = int((time.monotonic() - t0) * 1000)
        record_tool_call(
            self.session,
            adapter=self.llm.name,
            method="generate_listing",
            task_id=task.id,
            request={"product_id": product_db_id, "langs": langs, "site": self.settings.site, "attempts": attempts},
            response=None if not llm_res.ok else _usage_digest(llm_res.data),
            status="ok" if llm_res.ok else "failed",
            duration_ms=timings["llm_ms"],
        )
        if not llm_res.ok or llm_res.data is None:
            err = llm_res.error or AdapterError.generation("LLM_NO_RESULT", "文案生成失败且无错误体")
            payload = {**err.model_dump(), "attempts": attempts, "degraded_action": "manual_todo"}
            finish_task(self.session, task, status="failed", error=payload)
            self.session.commit()
            return {"task_id": task.id, "reused": False, "status": "failed", "error": payload, "timings": timings}

        outcome: LlmListingOutcome = llm_res.data
        usage_totals["prompt_tokens"] = outcome.usage.prompt_tokens
        usage_totals["completion_tokens"] = outcome.usage.completion_tokens
        usage_totals["calls"] += 1
        if outcome.degraded:
            degraded.append(
                {
                    "stage": "llm",
                    "error": {
                        "code": outcome.degrade_code or "LLM_FALLBACK_EXHAUSTED",
                        "message": "主备模型失败，已回落本地模板",
                    },
                }
            )

        # ---- 2. 三类信号分开（B4 实跑后重划的界）----
        #   compliance_flags = 词表硬命中 → 唯一可阻断信号（DESIGN §7.4/§7.5）
        #   advisory_flags   = 模型自查提示 → 给人参考，不参与阻断（会飘、会客套）
        #   leak_flags       = 供给侧信息泄漏（MOQ/批发/起订）→ 需人工处置
        # 混成一轨的后果实测出现过：模型把"洋气潮—主观用词"报成 flag，
        # 让正常童装文案三语种全被判违规。
        pkg = outcome.package
        leaked: list[str] = []
        warn_langs: list[str] = []
        all_hits_by_lang: dict[str, list[dict]] = {}
        for version in pkg.versions:
            body = {"title": version.title, "bullets": version.bullets, "description": version.description}
            hits = scan_listing_hits(body, site=self.settings.site)
            report = hits_to_report(hits)
            version.compliance_flags = sorted(set(report["block_terms"]))
            # warn 不进阻断清单；挂到 advisory 辅轨供人工参考（不参与 publishable）
            warn_terms = list(report["warn_terms"])
            if warn_terms:
                version.advisory_flags = sorted(
                    set(version.advisory_flags) | {f"compliance_warn:{t}" for t in warn_terms}
                )
                warn_langs.append(version.lang)
            all_hits_by_lang[version.lang] = [h.to_dict() for h in hits]
            version.leak_flags = sorted(set(scan_listing_leakage(body)))
            if version.leak_flags:
                leaked.append(version.lang)
        blocking = [v.lang for v in pkg.versions if v.compliance_flags]

        # ---- 3. 图片（A3 裁决：失败降级，不谎报） ----
        images_by_lang: dict[str, list[dict[str, Any]]] = {}
        t_img = time.monotonic()
        for version in pkg.versions:
            assets: list[dict[str, Any]] = []
            for ref in facts.main_images[: self.max_images_per_lang]:
                img_res = self.image.repaint(
                    ref,
                    version.lang,
                    # 把已生成的目标语言文案交给图像链路，图上不再是占位假字
                    overlay_headline=version.title,
                    overlay_sub=(version.bullets[0] if version.bullets else None),
                )
                record_tool_call(
                    self.session,
                    adapter=self.image.name,
                    method="repaint",
                    task_id=task.id,
                    request={"source_ref": ref, "lang": version.lang},
                    response=None if not img_res.ok else {"output_path": img_res.data.output_path, "mode": img_res.data.mode},
                    status="ok" if img_res.ok else "degraded",
                    duration_ms=img_res.duration_ms or None,
                )
                if img_res.ok and img_res.data is not None:
                    assets.append(
                        {
                            "source_ref": ref,
                            "output_path": img_res.data.output_path,
                            "mode": img_res.data.mode,
                            "needs_human": img_res.data.needs_human,
                            "watermark_check": img_res.data.watermark_check,
                            # 把适配器算出的原因一起存下来：只存 needs_human 布尔值的话，
                            # 抽检表就答不出"这张图为什么要人看"，人只能整包重审。
                            "needs_human_reasons": (img_res.data.notes or {}).get("needs_human_reasons", []),
                            "copy_is_placeholder": (img_res.data.notes or {}).get("copy_is_placeholder"),
                            "copy_rendered": (img_res.data.notes or {}).get("copy_rendered"),
                            # 告警也要冒泡：适配器说"检测器只覆盖右下角角标，浅色文字/
                            # 居中 logo/平铺水印本轮检不出"。不带上就会被误读成"确认无水印"。
                            "warnings": (img_res.data.notes or {}).get("warnings", []),
                        }
                    )
                else:
                    err = img_res.error or AdapterError.generation("IMG_NO_RESULT", "图像处理失败且无错误体")
                    fallback = (err.detail or {}).get("fallback", "original_kept")
                    assets.append({"source_ref": ref, "output_path": ref, "mode": fallback, "needs_human": True})
                    degraded.append({"stage": "image", "lang": version.lang, "error": err.model_dump()})
            images_by_lang[version.lang] = assets
        timings["image_ms"] = int((time.monotonic() - t_img) * 1000)
        timings["total_ms"] = timings["llm_ms"] + timings["image_ms"]

        # 图像产物自己的 needs_human 必须冒泡到包级判定。
        # 漏掉的后果实测出现过：TH 图标了需人工，包却报 needs_human=False，
        # 下游按包级字段放行 → A3"泰语图必须人工抽检"的约束被绕过。
        images_need_human = any(
            a.get("needs_human") for assets in images_by_lang.values() for a in assets
        )
        needs_human = (
            bool(degraded)
            or bool(blocking)
            or bool(leaked)
            or images_need_human
            or bool(outcome.degraded)
        )

        # ---- 2b. 政策拒绝：把 §7.4/§7.5 的"强制过滤"落成**可执行动作** ----
        # 之前只把词表命中记进 compliance_flags、折进 needs_human，
        # 然后照样存下违规草稿并把任务标成 succeeded —— "强制过滤"成了"记录一下"。
        # 更糟的是 COMPLIANCE_TERM_HIT / SUPPLIER_LEAK_HIT 登记为 policy_reject
        # （语义"不提供本次跳过"）却在整个 src 里从未被发出：
        # 登记表描述了一个谁都执行不了的拒绝（Review M3）。
        # 现在命中即：① 发出登记过的码 ② 整包标记不可发布 ③ 任务终态 failed。
        # 草稿仍落库 —— disposition 是 human_todo，人要拿它去改，不是要它消失。
        policy_rejections: list[dict[str, Any]] = []
        for version in pkg.versions:
            for code, terms in (
                ("COMPLIANCE_TERM_HIT", version.compliance_flags),
                ("SUPPLIER_LEAK_HIT", version.leak_flags),
            ):
                if not terms:
                    continue
                spec = resolve(code)
                entry: dict[str, Any] = {
                    "code": code,
                    "kind": (spec.kind.value if spec else ErrorKind.POLICY.value),
                    "disposition": (spec.disposition.value if spec else "policy_reject"),
                    "human_todo": bool(spec.human_todo) if spec else True,
                    "lang": version.lang,
                    "terms": sorted(set(terms)),
                }
                if code == "COMPLIANCE_TERM_HIT":
                    entry["hits"] = [
                        h for h in all_hits_by_lang.get(version.lang, []) if h.get("severity") == "block"
                    ]
                policy_rejections.append(entry)
        # 词表硬命中 = §7 硬规则 → 不可发布。
        # 供给侧泄漏是否升格为硬规则仍挂在 M1-PLAN §0.2 待拍板，
        # 所以 publishable 只看 blocking —— 不在未决政策上替 owner 拍板。
        publishable = not blocking
        payload = {
            "site": self.settings.site,
            "langs": [v.lang for v in pkg.versions],
            "versions": [v.model_dump() for v in pkg.versions],
            "images_by_lang": images_by_lang,
            "compliance": {
                "engine": "lexicon-primary+model-advisory",
                "lexicon": "assets/compliance_lexicon.json",
                "blocking_langs": blocking,
                "warn_langs": warn_langs,
                "leaked_langs": leaked,
                "hits_by_lang": all_hits_by_lang,
                "leak_note": "供给侧信息（MOQ/批发/起订/成本价）出现在买家文案，须人工删除后再上架",
                "report": pkg.compliance_report,
            },
            # 下游（M2 审批闸门）看这两个字段就知道能不能进闸门。
            "publishable": publishable,
            "policy_rejections": policy_rejections,
            "usage": {**usage_totals, "cost_amount": outcome.usage.cost_amount, "cost_note": "牌价折算非实扣；reasoning_tokens 不可用于核算（A2）"},
            "timings_ms": timings,
            "needs_human": needs_human,
            "degraded": degraded,
            "llm_attempts": attempts,
        }

        listing, _listing_created = get_or_create_listing(
            self.session,
            shop_id=self.shop_id,
            product_id=product_db_id,
            price=_suggest_price(facts),
            currency=facts.currency,
        )
        try:
            version_row = add_listing_version(self.session, listing.id, payload, created_by_task=task.id)
        except VersionConflictError as exc:
            # §4.5 的 CAS 重试耗尽。失败是数据：转人工待办，不把裸异常抛穿到 API 层变 500。
            err = AdapterError.execution("VERSION_CAS_EXHAUSTED", str(exc))
            finish_task(self.session, task, status="failed", error=err.model_dump())
            self.session.commit()
            return {
                "task_id": task.id,
                "reused": False,
                "status": "failed",
                "error": {**err.model_dump(), "degraded_action": "manual_todo"},
                "timings": timings,
            }

        digest = {
            "listing_id": listing.id,
            "listing_version_id": version_row.id,
            "version": version_row.version,
            "langs": payload["langs"],
            "needs_human": needs_human,
            "publishable": publishable,
            "timings_ms": timings,
        }
        if blocking:
            # 硬命中 → 任务终态 failed（policy_reject + human_todo），信封 ok=false。
            # 违规草稿仍可按 listing_version_id 取回给人改。
            err = AdapterError.policy(
                "COMPLIANCE_TERM_HIT",
                f"合规词硬命中，草稿不可作为可发布产物：语种 {blocking}",
                blocking_langs=blocking,
                rejections=policy_rejections,
                listing_version_id=version_row.id,
            )
            finish_task(self.session, task, status="failed", output=digest, error=err.model_dump())
            self.session.commit()
            return {
                "task_id": task.id,
                "reused": False,
                "status": "failed",
                "error": {**err.model_dump(), "degraded_action": "manual_todo"},
                "output": {"listing_id": listing.id, "listing_version_id": version_row.id, **payload},
                "timings": timings,
            }

        finish_task(self.session, task, status="succeeded", output=digest)
        self.session.commit()
        return {
            "task_id": task.id,
            "reused": False,
            "status": "succeeded",
            "output": {
                "listing_id": listing.id,
                "listing_version_id": version_row.id,
                "version": version_row.version,
                **payload,
            },
        }


def _usage_digest(outcome: LlmListingOutcome) -> dict[str, Any]:
    return {
        "langs": outcome.package.langs,
        "prompt_tokens": outcome.usage.prompt_tokens,
        "completion_tokens": outcome.usage.completion_tokens,
        "cost_amount": outcome.usage.cost_amount,
    }


def _suggest_price(facts: ProductFacts) -> int:
    """草稿建议价：成本 × 假设倍率。**标注为假设**，真实定价与净利归 Ledger（DESIGN §3.2）。"""
    if facts.price_cents is None:
        return 0
    return int(facts.price_cents * 3.0)
