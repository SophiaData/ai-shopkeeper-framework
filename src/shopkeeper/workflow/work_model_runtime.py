"""工作模型运行时：组装事实、调用 Provider、校验契约与护栏、留痕与持久化。

产品契约与安全：DESIGN §1.2.1 / §5；数据契约：SCHEMA 工作台 v14 operations_work_model_runs。
不直接暴露 run(raw, context) 给客户端自报可信上下文；
事实与版本摘要必须由服务端从数据库中检索并组装。
HTTP 调用 Provider 期间不持有 SQLite 写事务。
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from shopkeeper.workflow import work_models
from shopkeeper.workflow.providers import ChatProvider, build_provider
from shopkeeper.workflow.work_models import (
    CODE_INTERNAL,
    CODE_INVALID_JSON,
    PROMPT_VERSION,
    Module,
    WorkModelError,
    module_prompt,
    parse_output,
    system_prompt,
    validate_output,
)


class WorkModelRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    module: Module
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]
    object_id: Annotated[str, Field(max_length=128)] = ""
    scope: dict[str, Any] = Field(default_factory=dict)
    channel: str | None = None


class WorkModelRuntime:
    """工作模型服务端执行运行时。"""

    def __init__(self, workflow, *, provider: ChatProvider | None = None, store=None):
        self.workflow = workflow
        self.provider = provider
        self.store = store

    def compute_request_hash(self, req: WorkModelRunRequest) -> str:
        payload = {
            "module": req.module,
            "object_id": req.object_id,
            "scope": req.scope,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def assemble_context(
        self, shop_id: str, req: WorkModelRunRequest
    ) -> tuple[dict[str, Any], str, list[str], dict[str, Any]]:
        """从数据库读取可信事实并计算输入版本摘要；客户端不能自报核验或指标。

        第四个返回值是**服务端判定的关联上下文**（本次运行绑定的是哪条核验、
        哪条文案、哪个商品事实版本），只落库，不进入提示词，也不接受客户端自报。
        """
        context_dict: dict[str, Any] = {}
        input_object_ids: list[str] = []
        input_version_digest: str = ""
        linkage: dict[str, Any] = {}

        if req.object_id:
            input_object_ids = [req.object_id]
            if req.module in ("research", "verify", "write_copy"):
                with self.workflow._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                        (shop_id, req.object_id),
                    ).fetchone()
                if not row:
                    raise HTTPException(404, f"未找到商品候选对象：{req.object_id}")
                cand_data = json.loads(row["data_json"])
                input_version_digest = hashlib.sha256(
                    f"{row['data_json']}:{row['updated_at']}".encode()
                ).hexdigest()
                context_dict = {
                    "candidate_id": req.object_id,
                    "name": cand_data.get("name"),
                    "facts": cand_data.get("facts"),
                    "source_url": cand_data.get("source_url"),
                    "cost": cand_data.get("cost") or cand_data.get("cost_amount"),
                    "status": cand_data.get("status"),
                    "notes": cand_data.get("notes"),
                }
                if req.module == "write_copy":
                    with self.workflow._connect() as conn:
                        v_row = conn.execute(
                            "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND module='verify' AND status='succeeded' "
                            "AND input_object_ids_json LIKE ? ORDER BY created_at DESC LIMIT 1",
                            (shop_id, f'%"{req.object_id}"%'),
                        ).fetchone()
                    if v_row and v_row["output_json"]:
                        # 事实变动后旧核验不再为新版本背书：快照 digest 必须与当前事实完全一致
                        if v_row["input_version_digest"] == input_version_digest:
                            parsed_v = json.loads(v_row["output_json"])
                            if parsed_v.get("decision") == "pass":
                                context_dict["verify"] = parsed_v
                                linkage["verify_run_id"] = v_row["id"]
                    # allowed_text 是服务端认定的可用来源文本：候选事实 + 有效核验声明与证据。
                    # 正文里的绝对化/认证/功效措辞只有逐字出现在其中才算有支撑。
                    allowed = [str(cand_data.get("facts") or ""), str(cand_data.get("source_url") or "")]
                    verify_view = context_dict.get("verify")
                    if isinstance(verify_view, dict):
                        for claim in verify_view.get("claims") or []:
                            if not isinstance(claim, dict):
                                continue
                            if str(claim.get("status", "")).strip().lower() != "supported":
                                continue
                            allowed.append(str(claim.get("claim") or ""))
                            allowed.append(str(claim.get("evidence") or ""))
                    context_dict["allowed_text"] = "\n".join(part for part in allowed if part)
                    linkage["candidate_digest"] = input_version_digest

            elif req.module == "quality_check":
                # 质检绑定**实际文案版本**：先解析本次要质检的 write_copy 运行，
                # 再从数据库取候选事实、有效核验与版权来源，客户端不能自报这些事实。
                copy_run = self._resolve_copy_run(shop_id, req)
                with self.workflow._connect() as conn:
                    cand_row = conn.execute(
                        "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                        (shop_id, req.object_id),
                    ).fetchone()
                if not cand_row:
                    raise HTTPException(404, f"未找到质检对象：{req.object_id}")
                cand_data = json.loads(cand_row["data_json"])
                candidate_digest = hashlib.sha256(
                    f"{cand_row['data_json']}:{cand_row['updated_at']}".encode()
                ).hexdigest()
                if copy_run["input_version_digest"] != candidate_digest:
                    raise HTTPException(
                        409,
                        "版本冲突：文案对应的商品事实已发生变化，该文案不再作为当前事实的产物，请重新生成文案。",
                    )
                copy_output = copy_run["output"] or {}
                copy_text = work_models.copy_asset_text(copy_output)
                input_version_digest = work_models.copy_text_digest(copy_text)
                verify_view = self._current_verify(shop_id, req.object_id, candidate_digest)
                context_dict = {
                    "candidate_id": req.object_id,
                    "name": cand_data.get("name"),
                    "facts": cand_data.get("facts"),
                    "copy_run_id": copy_run["id"],
                    "copy": {
                        "title_th": copy_output.get("title_th"),
                        "body_th": copy_output.get("body_th"),
                        "title_en": copy_output.get("title_en"),
                        "body_en": copy_output.get("body_en"),
                    },
                    "copy_text": copy_text,
                    "copyright_source": str(cand_data.get("source_url") or "").strip(),
                    "facts_backed": bool(verify_view),
                }
                if verify_view:
                    context_dict["verify"] = verify_view
                linkage = {
                    "candidate_digest": candidate_digest,
                    "copy_run_id": copy_run["id"],
                    "copy_text_digest": input_version_digest,
                    "verify_run_id": (self._current_verify_run_id(shop_id, req.object_id, candidate_digest) or ""),
                }

            elif req.module == "record_metrics":
                context_dict, input_version_digest, linkage = self._assemble_metrics_context(
                    shop_id, req
                )
            elif req.module == "prepare_campaign":
                context_dict, input_version_digest, linkage = self._assemble_campaign_context(
                    shop_id, req
                )
            elif req.module == "orchestrator":
                context_dict, input_version_digest, linkage = self._assemble_orchestrator_context(
                    shop_id, req
                )
        else:
            if req.module == "record_metrics":
                context_dict, input_version_digest, linkage = self._assemble_metrics_context(
                    shop_id, req
                )
            elif req.module == "prepare_campaign":
                context_dict, input_version_digest, linkage = self._assemble_campaign_context(
                    shop_id, req
                )
            elif req.module == "orchestrator":
                context_dict, input_version_digest, linkage = self._assemble_orchestrator_context(
                    shop_id, req
                )
            else:
                input_version_digest = hashlib.sha256(
                    json.dumps(req.scope, sort_keys=True).encode()
                ).hexdigest()
                context_dict = dict(req.scope)

        return context_dict, input_version_digest, input_object_ids, linkage

    # ---- O-18：计划助手 / 指标复盘 / 总控建议的服务端可信上下文 ----

    def _assemble_campaign_context(
        self, shop_id: str, req: WorkModelRunRequest
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        """计划助手上下文：候选事实 + 本次输入的素材集合。

        客户端只能通过 ``scope.asset_ids`` 指出**本次输入**的素材；服务端逐个校验
        这些 ID 属于当前店铺且存在。模型返回的素材 ID 超出该集合时由护栏拒绝。

        候选是可选的：没有候选时只组装素材集合（供「先看素材再谈计划」的场景），
        此时事实相关字段留空，不臆造商品。
        """
        candidate_id = str(req.object_id or "").strip()
        candidate_digest = ""
        cand_data: dict[str, Any] = {}
        if candidate_id:
            with self.workflow._connect() as conn:
                cand_row = conn.execute(
                    "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                    (shop_id, candidate_id),
                ).fetchone()
            if not cand_row:
                raise HTTPException(404, f"未找到商品候选：{candidate_id}")
            cand_data = json.loads(cand_row["data_json"])
            candidate_digest = hashlib.sha256(
                f"{cand_row['data_json']}:{cand_row['updated_at']}".encode()
            ).hexdigest()

        requested = [str(x) for x in ((req.scope or {}).get("asset_ids") or [])]
        allowed_assets: list[dict[str, Any]] = []
        allowed_ids: list[str] = []
        if requested:
            with self.workflow._connect() as conn:
                for asset_id in requested:
                    asset_row = conn.execute(
                        "SELECT * FROM operations_assets WHERE shop_id=? AND id=?",
                        (shop_id, asset_id),
                    ).fetchone()
                    if not asset_row:
                        raise HTTPException(
                            404, f"未找到本次输入的素材：{asset_id}（不存在或不属于当前店铺）"
                        )
                    asset_data = json.loads(asset_row["data_json"])
                    allowed_assets.append(
                        {
                            "id": asset_id,
                            "name": asset_data.get("name"),
                            "language": asset_data.get("language"),
                            "kind": asset_data.get("kind"),
                            "status": asset_data.get("status"),
                            "candidate_id": asset_row["candidate_id"],
                            "text": asset_data.get("text") if asset_data.get("kind") == "text" else None,
                        }
                    )
                    allowed_ids.append(asset_id)

        verify_view = (
            self._current_verify(shop_id, candidate_id, candidate_digest) if candidate_id else None
        )
        context_dict = {
            "candidate_id": candidate_id,
            "name": cand_data.get("name"),
            "facts": cand_data.get("facts"),
            "source_url": cand_data.get("source_url"),
            "cost": cand_data.get("cost_amount"),
            "currency": cand_data.get("currency"),
            "allowed_assets": allowed_assets,
            "allowed_asset_ids": allowed_ids,
            "facts_backed": bool(verify_view),
        }
        if verify_view:
            context_dict["verify"] = verify_view
        input_version_digest = hashlib.sha256(
            json.dumps(
                {"candidate": candidate_digest, "assets": allowed_ids},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        linkage = {
            "candidate_digest": candidate_digest,
            "asset_ids": allowed_ids,
            "verify_run_id": (
                self._current_verify_run_id(shop_id, candidate_id, candidate_digest)
                if candidate_id
                else ""
            ),
        }
        return context_dict, input_version_digest, linkage

    def _assemble_metrics_context(
        self, shop_id: str, req: WorkModelRunRequest
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        """指标复盘上下文：服务端从数据库读出的基础指标。

        读取范围由服务端确定（计划 + 日期 + 来源 + 币种），模型只负责解读。
        不同币种不得合并：范围内出现多币种时直接拒绝，不让模型在混合口径上复盘。
        """
        scope = req.scope or {}
        object_id = str(req.object_id or scope.get("campaign_id") or "").strip()
        if not object_id:
            raise HTTPException(422, "复盘必须指定投放计划或商品候选（campaign_id）")

        with self.workflow._connect() as conn:
            campaign_row = conn.execute(
                "SELECT * FROM operations_campaigns WHERE shop_id=? AND id=?",
                (shop_id, object_id),
            ).fetchone()
        plan_ids: list[str] = []
        anchor_campaign_id = object_id
        campaign_data: dict[str, Any] = {}
        if campaign_row:
            plan_ids = [object_id]
            campaign_data = json.loads(campaign_row["data_json"])
        else:
            # 对象可能是**商品候选**：复盘该候选名下全部计划的指标。
            with self.workflow._connect() as conn:
                cand_row = conn.execute(
                    "SELECT 1 FROM operations_candidates WHERE shop_id=? AND id=?",
                    (shop_id, object_id),
                ).fetchone()
            if not cand_row:
                raise HTTPException(404, f"未找到投放计划或商品候选：{object_id}")
            with self.workflow._connect() as conn:
                plan_rows = conn.execute(
                    "SELECT id FROM operations_campaigns WHERE shop_id=? AND candidate_id=?",
                    (shop_id, object_id),
                ).fetchall()
            plan_ids = [str(r["id"]) for r in plan_rows]

        day_from = str(scope.get("from") or "").strip()
        day_to = str(scope.get("to") or "").strip()
        source = str(scope.get("source") or "").strip()

        metrics_list: list[dict[str, Any]] = []
        if plan_ids:
            placeholders = ",".join("?" for _ in plan_ids)
            where = f"shop_id=? AND campaign_id IN ({placeholders})"
            params: list[Any] = [shop_id, *plan_ids]
            if day_from:
                where += " AND day>=?"
                params.append(day_from)
            if day_to:
                where += " AND day<=?"
                params.append(day_to)
            if source:
                where += " AND source=?"
                params.append(source)
            with self.workflow._connect() as conn:
                m_rows = conn.execute(
                    f"SELECT * FROM operations_metrics WHERE {where} ORDER BY day ASC, currency ASC",
                    params,
                ).fetchall()
            metrics_list = [dict(r) for r in m_rows]

        currencies = sorted({str(r["currency"]) for r in metrics_list})
        if len(currencies) > 1:
            raise HTTPException(
                422,
                "复盘范围包含多个币种（" + "、".join(currencies) + "）：不同币种不得合并，请按币种分别复盘。",
            )
        currency = currencies[0] if currencies else None

        # 服务端读到的原始指标：按「列全为 NULL 才算缺失」的口径求和，
        # 不把缺失补成 0（补 0 等于宣称「当天确实为零」）。
        totals: dict[str, Any] = {}
        for measure in ("impressions", "clicks", "spend", "revenue", "orders"):
            values = [r[measure] for r in metrics_list]
            if not values or all(v is None for v in values):
                totals[measure] = None
                continue
            if any(v is None for v in values):
                # 部分行缺失时该列整体按未知处理，不能只加已知行。
                totals[measure] = None
                continue
            totals[measure] = sum(values)

        server_metrics: dict[str, Any] = {}
        if currency:
            server_metrics[currency] = totals
        query_scope = {
            "campaign_id": anchor_campaign_id,
            "campaign_ids": plan_ids,
            "from": day_from or None,
            "to": day_to or None,
            "source": source or None,
            "currency": currency,
        }
        input_version_digest = hashlib.sha256(
            json.dumps(
                {"metrics": metrics_list, "scope": query_scope},
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        context_dict = {
            "campaign_id": anchor_campaign_id,
            "campaign": campaign_data,
            "campaign_ids": plan_ids,
            "metrics": metrics_list,
            "server_metrics": server_metrics,
            "query_scope": query_scope,
            "scope": req.scope,
        }
        linkage = {
            "campaign_id": anchor_campaign_id,
            "campaign_ids": plan_ids,
            "metrics_digest": input_version_digest,
            "query_scope": query_scope,
            "server_metrics": server_metrics,
        }
        return context_dict, input_version_digest, linkage

    def _assemble_orchestrator_context(
        self, shop_id: str, req: WorkModelRunRequest
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        """总控上下文：服务端从当前店铺状态生成 gate，模型只给建议。

        gate 的每个输入都由服务端判定（是否存在候选/有效核验/可用素材/计划/
        指标、是否跨店、是否涉及金额或 PII、币种是否明确），客户端不能自报
        「事实充分」或「已通过审核」。
        """
        scope = req.scope or {}
        candidate_id = str(req.object_id or scope.get("candidate_id") or "").strip()
        facts: dict[str, Any] = {}
        if candidate_id:
            with self.workflow._connect() as conn:
                cand_row = conn.execute(
                    "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                    (shop_id, candidate_id),
                ).fetchone()
            if not cand_row:
                raise HTTPException(404, f"未找到商品候选：{candidate_id}")
            cand_data = json.loads(cand_row["data_json"])
            candidate_digest = hashlib.sha256(
                f"{cand_row['data_json']}:{cand_row['updated_at']}".encode()
            ).hexdigest()
            verify_view = self._current_verify(shop_id, candidate_id, candidate_digest)
            with self.workflow._connect() as conn:
                asset_rows = conn.execute(
                    "SELECT data_json FROM operations_assets WHERE shop_id=? AND candidate_id=?",
                    (shop_id, candidate_id),
                ).fetchall()
            usable_assets = []
            for asset_row in asset_rows:
                asset_data = json.loads(asset_row["data_json"])
                if asset_data.get("status") == "usable":
                    usable_assets.append(asset_data.get("name"))
            with self.workflow._connect() as conn:
                campaign_rows = conn.execute(
                    "SELECT data_json FROM operations_campaigns WHERE shop_id=? AND candidate_id=?",
                    (shop_id, candidate_id),
                ).fetchall()
            campaign_data_list = [json.loads(r["data_json"]) for r in campaign_rows]
            with self.workflow._connect() as conn:
                metric_count = conn.execute(
                    "SELECT count(*) FROM operations_metrics m JOIN operations_campaigns c "
                    "ON m.shop_id=c.shop_id AND m.campaign_id=c.id WHERE m.shop_id=? AND c.candidate_id=?",
                    (shop_id, candidate_id),
                ).fetchone()[0]
            facts = {
                "candidate_id": candidate_id,
                "name": cand_data.get("name"),
                "has_facts": bool(str(cand_data.get("facts") or "").strip()),
                "has_copyright_source": bool(str(cand_data.get("source_url") or "").strip()),
                "has_valid_verify": bool(verify_view),
                "usable_asset_count": len(usable_assets),
                "campaign_count": len(campaign_data_list),
                "metric_row_count": int(metric_count),
                "currency_known": bool(str(cand_data.get("currency") or "").strip()),
            }
        else:
            with self.workflow._connect() as conn:
                candidate_count = conn.execute(
                    "SELECT count(*) FROM operations_candidates WHERE shop_id=?", (shop_id,)
                ).fetchone()[0]
            facts = {"candidate_count": int(candidate_count)}

        gate_inputs = {
            "parseable": True,
            "cross_shop": False,
            "involves_money_or_pii": bool(scope.get("involves_money_or_pii")),
            "facts_sufficient": bool(facts.get("has_facts", True)),
            "sensitive_claims": bool(scope.get("sensitive_claims")),
            "copyright_missing": not bool(facts.get("has_copyright_source", True)),
            "currency_unknown": not bool(facts.get("currency_known", True)),
        }
        decision = work_models.route(**gate_inputs)
        # 上下文的 ``gate`` 用扁平结构（护栏按 route() 的入参名读取），
        # 裁决结论单独放 ``gate_decision``，两者都不接受客户端自报。
        context_dict = {
            "facts": facts,
            "gate": gate_inputs,
            "gate_decision": decision,
            "scope": req.scope,
        }
        input_version_digest = hashlib.sha256(
            json.dumps({"facts": facts, "gate": gate_inputs}, sort_keys=True, default=str).encode()
        ).hexdigest()
        linkage = {
            "gate_decision": decision,
            "gate_inputs": gate_inputs,
            "candidate_id": candidate_id,
        }
        return context_dict, input_version_digest, linkage

    def _resolve_copy_run(self, shop_id: str, req: WorkModelRunRequest) -> dict[str, Any]:
        """解析本次要质检的 write_copy 运行记录。

        优先用请求里的 ``scope.copy_run_id``（同一店铺内）；缺省时取该候选最近一条
        成功的 write_copy 运行。两者的正文都由服务端从数据库读取。
        """
        copy_run_id = str((req.scope or {}).get("copy_run_id") or "").strip()
        with self.workflow._connect() as conn:
            if copy_run_id:
                row = conn.execute(
                    "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND id=? AND module='write_copy'",
                    (shop_id, copy_run_id),
                ).fetchone()
                if not row:
                    raise HTTPException(404, f"未找到文案运行记录：{copy_run_id}")
            else:
                row = conn.execute(
                    "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND module='write_copy' AND status='succeeded' "
                    "AND input_object_ids_json LIKE ? ORDER BY created_at DESC LIMIT 1",
                    (shop_id, f'%"{req.object_id}"%'),
                ).fetchone()
                if not row:
                    raise HTTPException(
                        404,
                        "未找到该商品候选的文案运行记录：先生成文案再发起质检。",
                    )
        run = self.store.run_view(row)
        if req.object_id not in (run.get("input_object_ids") or []):
            raise HTTPException(422, "该文案运行记录不属于此商品候选")
        if run.get("status") != "succeeded" or not run.get("output"):
            raise HTTPException(422, "文案运行记录未成功或无输出，不能作为质检对象")
        return run

    def _current_verify(self, shop_id: str, candidate_id: str, candidate_digest: str):
        """当前候选版本对应的有效核验结论；事实变动后旧核验不再背书。"""
        run_id = self._current_verify_run_id(shop_id, candidate_id, candidate_digest)
        if not run_id:
            return None
        with self.workflow._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND id=?",
                (shop_id, run_id),
            ).fetchone()
        if not row or not row["output_json"]:
            return None
        parsed = json.loads(row["output_json"])
        if parsed.get("decision") != "pass":
            return None
        claims = [
            claim
            for claim in parsed.get("claims") or []
            if isinstance(claim, dict)
            and str(claim.get("status", "")).strip().lower() == "supported"
            and str(claim.get("evidence") or "").strip()
        ]
        if not claims:
            return None
        return parsed

    def _current_verify_run_id(self, shop_id: str, candidate_id: str, candidate_digest: str) -> str | None:
        """当前候选版本对应的有效核验运行 ID；无有效核验返回 None。"""
        with self.workflow._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND module='verify' AND status='succeeded' "
                "AND input_object_ids_json LIKE ? ORDER BY created_at DESC LIMIT 1",
                (shop_id, f'%"{candidate_id}"%'),
            ).fetchone()
        if not row or not row["output_json"]:
            return None
        if row["input_version_digest"] != candidate_digest:
            return None
        parsed = json.loads(row["output_json"])
        if parsed.get("decision") != "pass":
            return None
        claims = [
            claim
            for claim in parsed.get("claims") or []
            if isinstance(claim, dict)
            and str(claim.get("status", "")).strip().lower() == "supported"
            and str(claim.get("evidence") or "").strip()
        ]
        if not claims:
            return None
        return str(row["id"])

    def run_module(self, shop_id: str, req: WorkModelRunRequest) -> dict[str, Any]:
        """执行工作模型调用链路。

        1. 检查幂等与同键异载荷冲突
        2. 校验日预算
        3. 组装事实与提示词
        4. Provider 外呼（不持 SQLite 写锁）
        5. 严格 JSON 解析与护栏验证
        6. 双重留痕（调用表 + 结果运行表）
        """
        if self.store is None:
            from shopkeeper.workflow.operations import OperationsStore
            self.store = OperationsStore(self.workflow)

        request_hash = self.compute_request_hash(req)

        # 1. 幂等与重入判定
        existing = self.store.get_run_by_idempotency_key(shop_id, req.idempotency_key)
        if existing:
            if existing.get("request_hash") == request_hash:
                return existing
            raise HTTPException(409, "同键异载荷拒绝：该 idempotency_key 已关联不同请求内容")

        # 2. 对象验证与服务端事实提取（跨店直接 404，不调 provider）
        context_dict, input_version_digest, input_object_ids, linkage = self.assemble_context(shop_id, req)

        # 3. 预算检查（超额 409，不调 provider）
        from shopkeeper.orchestrator.budget import check_budget, daily_budget_usd
        from shopkeeper.orchestrator.store import OrchestratorStore

        if daily_budget_usd() is not None:
            try:
                spent = float(OrchestratorStore(self.workflow).spent_usd_today())
            except Exception:
                spent = 0.0
            gate = check_budget(spent, env=None, emit=True, run_id="")
            if not gate.get("ok", True):
                raise HTTPException(
                    409,
                    f"LLM 日预算已用尽，工作模型调用已拒绝（已用 {gate.get('spent')} / 上限 {gate.get('limit')} USD）。",
                )

        # 4. 组装提示词
        sys_prompt = system_prompt()
        mod_prompt = module_prompt(req.module)
        user_prompt = (
            f"模块任务说明：\n{mod_prompt}\n\n"
            f"业务事实与上下文（来自服务端数据库）：\n{json.dumps(context_dict, ensure_ascii=False, indent=2)}"
        )

        # 5. 调用 Provider（无写锁）
        provider = self.provider or build_provider()
        model_channel = req.channel or getattr(provider, "name", "mock")

        raw_output = ""
        call_failure_reason: str | None = None
        run_failure_code: str | None = None
        parsed_output: dict[str, Any] | None = None

        try:
            res = provider.complete(sys_prompt, user_prompt)
            if not res.ok:
                err = res.error
                run_failure_code = err.code if err else "LLM_PROVIDER_ERROR"
                call_failure_reason = f"{run_failure_code}: {err.message if err else 'Provider failed'}"
                raw_output = ""
            else:
                raw_output = res.data or ""
                # MockChatProvider 返回空字符串时，使用模块级确定性合法响应
                if getattr(provider, "name", "") == "mock" and not raw_output.strip():
                    raw_output = work_models.mock_output(req.module)
                elif not raw_output.strip():
                    run_failure_code = CODE_INVALID_JSON
                    call_failure_reason = f"{CODE_INVALID_JSON}: 模型返回空响应"
        except httpx.TimeoutException:
            run_failure_code = "LLM_TIMEOUT"
            call_failure_reason = "LLM_TIMEOUT: 对话接口超时"
            raw_output = ""
        except Exception as exc:
            run_failure_code = "LLM_HTTP_ERROR"
            call_failure_reason = f"LLM_HTTP_ERROR: {type(exc).__name__}: {exc}"
            raw_output = ""

        # 6. 解析与护栏（如果前面没有 provider 级错误）
        if run_failure_code is None:
            try:
                parsed_record = parse_output(req.module, raw_output)
                guard_codes = validate_output(req.module, parsed_record, context_dict)
                if guard_codes:
                    run_failure_code = guard_codes[0]
                    call_failure_reason = f"{guard_codes[0]}: 模型输出未通过护栏：" + ", ".join(guard_codes)
                else:
                    parsed_output = work_models.dump_output(parsed_record)
            except WorkModelError as exc:
                run_failure_code = exc.code
                call_failure_reason = f"{exc.code}: {exc.message}"
            except Exception as exc:
                run_failure_code = CODE_INTERNAL
                call_failure_reason = f"{CODE_INTERNAL}: {type(exc).__name__}: {exc}"

        # 7. 审计留痕 operations_work_model_calls
        call_record_id: str | None = None
        try:
            call_res = self.store.record_call(
                shop_id,
                {
                    "module": req.module,
                    "prompt_version": PROMPT_VERSION,
                    "model_channel": model_channel,
                    "input_object_ids": input_object_ids,
                    "output_object_ids": [],
                    "source": "api",
                    "human_review": False,
                    "human_changes": [],
                },
                failure_reason=call_failure_reason,
            )
            call_record_id = call_res.get("id")
        except Exception:
            pass

        # 8. 运行结果持久化 operations_work_model_runs
        status = "succeeded" if run_failure_code is None and parsed_output is not None else "failed"
        run_record = self.store.record_run(
            shop_id,
            {
                "module": req.module,
                "idempotency_key": req.idempotency_key,
                "request_hash": request_hash,
                "status": status,
                "prompt_version": PROMPT_VERSION,
                "model_channel": model_channel,
                "input_version_digest": input_version_digest,
                "input_object_ids": input_object_ids,
                "output": parsed_output,
                "raw_output": raw_output,
                "call_record_id": call_record_id,
                "context": linkage,
            },
            failure_code=run_failure_code,
            failure_reason=call_failure_reason,
        )

        return run_record
